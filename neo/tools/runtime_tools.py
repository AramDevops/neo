from __future__ import annotations

import json
import os
import re
import socket
import subprocess
from pathlib import Path
from typing import Dict

from ..config import Settings
from .base import ToolResult, ToolboxHelpers


class RuntimeTools(ToolboxHelpers):
    """Project probing, port intelligence, and managed process lifecycle.

    Started is not serving: _start_process only reports ok when the child's
    expected port listens or it survives the readiness window (ProcessManager
    owns that gate); failures carry stdout/stderr evidence for self-repair.
    """

    def _project_probe(self, path: str) -> ToolResult:
        root = self._safe_path(path or ".")
        if not root.exists() or not root.is_dir():
            return ToolResult(False, "project_probe", f"Project path is not a directory: {path}", {"path": str(root)})

        common_files = {
            name: (root / name).exists()
            for name in [
                "requirements.txt",
                "pyproject.toml",
                "Pipfile",
                "poetry.lock",
                "manage.py",
                "app.py",
                "wsgi.py",
                "package.json",
                "vite.config.js",
                "vite.config.ts",
            ]
        }
        package_scripts: dict[str, str] = {}
        package_path = root / "package.json"
        if package_path.exists():
            try:
                package = json.loads(package_path.read_text(encoding="utf-8"))
                scripts = package.get("scripts") if isinstance(package, dict) else {}
                if isinstance(scripts, dict):
                    package_scripts = {str(key): str(value) for key, value in scripts.items()}
            except Exception:
                package_scripts = {}

        python_project = any(common_files[name] for name in ["requirements.txt", "pyproject.toml", "Pipfile", "poetry.lock", "manage.py", "app.py", "wsgi.py"])
        node_project = common_files["package.json"]
        node_missing, _node_has_deps = self._node_missing_dependencies(root) if node_project else ([], False)
        venv_candidates = [
            str((root / rel).relative_to(self.workspace))
            for rel in [".neo/venv", ".venv", "venv"]
            if (root / rel).exists()
        ]
        suggested_ports = self._suggest_project_ports(root, common_files, package_scripts)
        payload = {
            "path": str(root),
            "relative_path": str(root.relative_to(self.workspace)),
            "python_project": python_project,
            "node_project": node_project,
            "common_files": common_files,
            "package_scripts": package_scripts,
            # Install state, read up front: installs must be decisions based
            # on what exists, not reflexes (runs 102/103/106 re-ran npm
            # install on an already-installed project every single time).
            "node_modules_exists": node_project and (root / "node_modules").exists(),
            "node_dependencies_installed": node_project and not node_missing,
            "node_missing_dependencies": node_missing[:20],
            "python_isolation": {
                "recommended": python_project and not venv_candidates,
                "existing_venvs": venv_candidates,
                "suggested_venv_path": ".neo/venv",
                "requirements": "requirements.txt" if common_files["requirements.txt"] else "",
            },
            "suggested_ports": suggested_ports,
            "port_checks": [self._port_snapshot(port) for port in suggested_ports],
        }
        return ToolResult(True, "project_probe", json.dumps(payload, indent=2, default=str), payload)

    def _suggest_project_ports(self, root: Path, common_files: dict[str, bool], package_scripts: dict[str, str]) -> list[int]:
        detected = self._ports_from_project_files(root, common_files, package_scripts)
        if detected:
            return sorted(detected)

        ports: set[int] = set()
        if any(common_files.get(name) for name in ["app.py", "wsgi.py"]) or any("flask" in value.lower() for value in package_scripts.values()):
            ports.add(5000)
        if common_files.get("manage.py") or common_files.get("pyproject.toml"):
            ports.add(8000)
        if common_files.get("package.json"):
            joined_scripts = " ".join(package_scripts.values()).lower()
            if "vite" in joined_scripts or common_files.get("vite.config.js") or common_files.get("vite.config.ts"):
                ports.add(5173)
            if any(marker in joined_scripts for marker in ["next", "react-scripts", "node", "server"]):
                ports.add(3000)
        return sorted(ports or {5000, 8000, 5173, 3000})

    def _port_check(self, port: int) -> ToolResult:
        if port <= 0 or port > 65535:
            return ToolResult(False, "port_check", "Port must be between 1 and 65535.", {"port": port})
        listener = self._port_listener(port)
        if listener:
            return ToolResult(False, "port_check", self._port_conflict_text(listener), {"port": port, "available": False, "listener": listener})
        return ToolResult(True, "port_check", f"Port {port} is available on localhost.", {"port": port, "available": True})

    def _find_free_port(self, start: int, end: int) -> ToolResult:
        start = max(1, min(start, 65535))
        end = max(start, min(end, 65535))
        for port in range(start, end + 1):
            if not self._port_listener(port) and self._can_bind_port(port):
                return ToolResult(True, "find_free_port", f"Port {port} is available.", {"port": port, "range": [start, end]})
        return ToolResult(False, "find_free_port", f"No free localhost port found between {start} and {end}.", {"range": [start, end]})

    def _app_healthcheck(self, url: str, path: str, raw_checks: list) -> ToolResult:
        """Prove the app works, deterministically. ok=True ONLY when the root
        page serves, every referenced asset serves with a sane content type,
        every declared acceptance check passes, and (when a project path is
        given) no require()'d Node package is missing. The model declares what
        success looks like; this tool executes it; a feature claim without a
        passing health check covering it is not evidence."""
        from ..services import app_check

        clean_url = (url or "").strip()
        if not clean_url.startswith(("http://", "https://")):
            return ToolResult(False, "app_healthcheck", "A full http(s) URL is required, e.g. http://127.0.0.1:3003.", {"url": clean_url})

        results: list[dict] = []
        if (path or "").strip():
            root = self._safe_path(path)
            for package in app_check.node_dependency_gaps(root):
                results.append({
                    "name": f"dependency:{package}",
                    "ok": False,
                    "detail": (
                        f"'{package}' is required by the code but is neither a Node builtin nor installed "
                        "in node_modules: the server will crash with \"Cannot find module\". Install it "
                        "with node_install, or rewrite the code to use built-in modules."
                    ),
                })

        report = app_check.run_checks(clean_url, raw_checks)
        report["results"] = [*results, *report["results"]]
        failed = [item for item in report["results"] if not item["ok"]]
        report["ok"] = not failed
        report["summary"] = (
            f"{len(report['results']) - len(failed)}/{len(report['results'])} checks passed"
            + ("" if not failed else "; FAILING: " + "; ".join(f"{item['name']} ({item['detail']})" for item in failed[:5]))
        )
        lines = [f"App health check for {clean_url}: {'HEALTHY' if report['ok'] else 'FAILING'}"]
        lines += [f"- {'PASS' if item['ok'] else 'FAIL'} {item['name']}: {item['detail']}" for item in report["results"]]
        if failed:
            lines.append("Fix exactly the FAIL lines above, then run app_healthcheck again until every check passes.")
        return ToolResult(bool(report["ok"]), "app_healthcheck", "\n".join(lines), report)

    def _start_process(
        self,
        command: str,
        name: str,
        path: str = "",
        venv_path: str = "",
        port: int = 0,
        wait_seconds: float = 0,
    ) -> ToolResult:
        if not Settings.shell_enabled:
            return ToolResult(False, "start_process", "Process tool is disabled.", {})
        command = command.strip()
        if not command:
            return ToolResult(False, "start_process", "Command is required.", {})
        blocked = self._blocked_command(command)
        if blocked:
            return ToolResult(False, "start_process", "Command blocked by harness policy.", {"command": command, "blocked": blocked})

        cwd = self._safe_path(path or ".")
        if not cwd.is_dir():
            return ToolResult(False, "start_process", f"Working directory does not exist: {path}", {"command": command, "path": path})

        env: Dict[str, str] = {}
        if venv_path:
            venv_root = self._safe_child_path(cwd, venv_path)
            scripts_dir = venv_root / ("Scripts" if os.name == "nt" else "bin")
            if not scripts_dir.is_dir():
                return ToolResult(
                    False,
                    "start_process",
                    f"venv_path has no interpreter directory: {scripts_dir}. Create it first with python_install or python_venv.",
                    {"command": command, "venv_path": venv_path},
                )
            env["PATH"] = f"{scripts_dir}{os.pathsep}{os.environ.get('PATH', '')}"
            env["VIRTUAL_ENV"] = str(venv_root)

        ports = self._ports_for_command(command, cwd)
        if port:
            ports = sorted({*ports, port})
        if not ports:
            probe_ports = self._ports_from_python_file(cwd / "app.py")
            ports = sorted(probe_ports)
        port_conflicts = [self._port_listener(candidate) for candidate in ports]
        port_conflicts = [conflict for conflict in port_conflicts if conflict]
        reclaimed_ports: list[int] = []
        if port_conflicts:
            # Reclaim listeners Neo itself started (including orphans from a
            # previous backend session, via the persisted registry). Foreign
            # listeners are never touched; those conflicts stay fatal and
            # the caller must route around them.
            remaining = []
            for conflict in port_conflicts:
                conflict_port = int(conflict.get("port") or 0)
                conflict_pid = int(conflict.get("pid") or 0) or None
                if conflict_port and self.processes.reclaim_port(conflict_port, conflict_pid):
                    reclaimed_ports.append(conflict_port)
                else:
                    remaining.append(conflict)
            port_conflicts = remaining
        if port_conflicts:
            return ToolResult(
                False,
                "start_process",
                "Port preflight failed before starting process.\n" + "\n".join(self._port_conflict_text(conflict) for conflict in port_conflicts),
                {"command": command, "port_conflicts": port_conflicts, "reclaimed_ports": reclaimed_ports},
            )

        info = self.processes.start(
            command=command,
            name=name,
            cwd=cwd,
            expected_ports=ports,
            env=env or None,
            wait_seconds=wait_seconds if wait_seconds > 0 else 12.0,
        )
        payload = {key: value for key, value in info.items() if key not in {"started_at"}}
        payload["path"] = str(cwd)
        if reclaimed_ports:
            payload["reclaimed_ports"] = reclaimed_ports
        if not info.get("ready"):
            detail = str(info.get("reason") or "process did not become ready")
            evidence = (info.get("stderr_tail") or info.get("stdout_tail") or "").strip()
            output = f"{info.get('name')} failed to start: {detail}"
            if evidence:
                output += f"\n\nProcess output (evidence for the fix):\n{evidence[-1500:]}"
            return ToolResult(False, "start_process", output, payload)
        readiness = str(info.get("reason") or "")
        listening = info.get("listening_port")
        summary = f"Started {info.get('name')} as pid {info.get('pid')}"
        if listening:
            summary += f", verified listening on port {listening}"
        elif readiness:
            summary += f" ({readiness})"
        return ToolResult(True, "start_process", summary, payload)

    def _process_status(self, pid: int, stdout_log: str = "", stderr_log: str = "") -> ToolResult:
        if pid <= 0:
            return ToolResult(False, "process_status", "A valid pid is required.", {})
        running = self.processes.is_running(pid)

        logs = [path for path in [self._safe_artifact_log(stdout_log), self._safe_artifact_log(stderr_log)] if path]
        if not logs:
            entry = self.processes.registry_entry(pid)
            if entry:
                logs = [path for path in [self._safe_artifact_log(entry.get("stdout_log", "")), self._safe_artifact_log(entry.get("stderr_log", ""))] if path]
        payload = {
            "pid": pid,
            "running": running,
            "logs": [str(path) for path in logs[-4:]],
            "log_tails": {path.name: self._tail_file(path) for path in logs[-4:]},
        }
        status = "running" if running else "not running"
        output = f"Process {pid} is {status}."
        for path in logs[-2:]:
            tail = self._tail_file(path, 1200).strip()
            if tail:
                output += f"\n\n{path.name}:\n{tail}"
        return ToolResult(running, "process_status", output, payload)

    def _stop_process(self, pid: int) -> ToolResult:
        from .base import current_run_context

        # Two deterministic guards. (1) Only Neo-managed processes can be
        # stopped - this tool must never be a kill-any-pid-on-the-machine
        # primitive. (2) A live process owned by ANOTHER agent is off-limits:
        # in a team, stopping a sibling's server breaks their verified work.
        running = pid > 0 and self.processes.is_running(pid)
        if running and not self.processes.owns_pid(pid):
            return ToolResult(False, "stop_process", (
                f"Refused: process {pid} was not started by Neo. Only Neo-managed processes can be stopped; "
                "use list_processes to see them."
            ), {"pid": pid, "refused": "not_managed"})
        # Ownership resolved through the registered ANCESTOR: a listener is
        # usually a child of the shell wrapper, so a bare registry lookup on
        # the listener pid would miss the owner and let a sibling kill it.
        owner_info = self.processes.owner_agent_of(pid) or {}
        owner = owner_info.get("owner_agent_id")
        requester = current_run_context().get("agent_id")
        if (
            running
            and owner is not None
            and requester is not None
            and int(owner) != int(requester)
        ):
            owner_name = owner_info.get("owner_agent_name") or f"agent {owner}"
            return ToolResult(False, "stop_process", (
                f"Refused: process {pid} ({owner_info.get('name')}) is owned by {owner_name} and is still serving. "
                "Do not stop another agent's process; if you need its port, choose a different one with find_free_port."
            ), {"pid": pid, "refused": "foreign_owner", "owner_agent_id": owner})
        result = self.processes.stop(pid)
        stopped = bool(result.get("stopped"))
        reason = str(result.get("reason") or "")
        output = f"Process {pid}: {reason}" if reason else (f"Stopped process {pid}." if stopped else f"Could not stop process {pid}.")
        return ToolResult(stopped or not result.get("running", False), "stop_process", output, {"pid": pid, **result})

    def _list_processes(self) -> ToolResult:
        entries = self.processes.list_managed()
        if not entries:
            return ToolResult(True, "list_processes", "No background processes started by Neo are currently running.", {"processes": []})
        lines = []
        for entry in entries:
            state = "running" if entry.get("running") else "exited"
            lines.append(f"- pid {entry.get('pid')} [{state}] {entry.get('name')}: {entry.get('command')} (cwd {entry.get('cwd')})")
        return ToolResult(True, "list_processes", "\n".join(lines), {"processes": entries})

    def _port_snapshot(self, port: int) -> dict:
        listener = self._port_listener(port)
        return {
            "port": port,
            "available": listener is None,
            "listener": listener,
        }

    def _ports_for_command(self, command: str, cwd: Path | None = None) -> list[int]:
        lowered = command.lower()
        found: list[int] = []
        patterns = [
            r"(?:--port|-p)\s*(?:=|\s)\s*(\d{1,5})",
            r"\bport\s*=\s*(\d{1,5})",
            r"\$env:port\s*=\s*['\"]?(\d{1,5})",
            r"(?:localhost|127\.0\.0\.1|0\.0\.0\.0):(\d{1,5})",
            r"\brunserver\s+(?:[0-9.]+:)?(\d{1,5})",
            r"\bhttp\.server\s+(\d{1,5})",
        ]
        for pattern in patterns:
            for match in re.finditer(pattern, lowered, re.I):
                found.append(int(match.group(1)))
        found.extend(self._ports_from_command_scripts(command, cwd))
        if not found:
            if "flask run" in lowered or re.search(r"\bpython(?:\.\w+)?(?:\s+\S+)*\s+\S*app\.py\b", lowered):
                found.append(5000)
            elif "manage.py runserver" in lowered or "uvicorn" in lowered or "gunicorn" in lowered or "python -m http.server" in lowered:
                found.append(8000)
            elif "vite" in lowered or "npm run dev" in lowered or "yarn dev" in lowered or "pnpm dev" in lowered:
                found.append(5173)
            elif "npm start" in lowered or re.search(r"\bnode(?:\s+\S+)*\s+\S*server\.js\b", lowered):
                found.append(3000)
        return sorted({port for port in found if 1 <= port <= 65535})

    def _ports_from_project_files(self, root: Path, common_files: dict[str, bool], package_scripts: dict[str, str]) -> set[int]:
        ports: set[int] = set()
        for script in package_scripts.values():
            ports.update(self._ports_for_command(script))
        for name in ["app.py", "wsgi.py", "manage.py"]:
            if common_files.get(name):
                ports.update(self._ports_from_python_file(root / name))
        return ports

    def _ports_from_command_scripts(self, command: str, cwd: Path | None = None) -> set[int]:
        ports: set[int] = set()
        script_match = re.search(
            r"(?:^|[\s;&|])(?:[\"']?(?:[\w.:-]+[\\/])*(?:python|python\d+(?:\.\d+)?|python\.exe|py)[\"']?)"
            r"(?:\s+-[^\s]+)*\s+[\"']?([^\"'\s;&|]+\.py)[\"']?",
            command,
            re.I,
        )
        if not script_match:
            return ports
        raw = script_match.group(1)
        # The script path is relative to the process working directory first;
        # resolving only against the workspace root missed subproject apps and
        # made the preflight veto launches over heuristic default ports the
        # app never uses.
        candidates = []
        if cwd is not None:
            candidates.append(lambda: self._safe_child_path(cwd, raw))
        candidates.append(lambda: self._safe_path(raw))
        for resolve in candidates:
            try:
                declared = self._ports_from_python_file(resolve())
            except Exception:
                continue
            if declared:
                ports.update(declared)
                break
        return ports

    def _ports_from_python_file(self, path: Path) -> set[int]:
        if not path.exists() or not path.is_file():
            return set()
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except Exception:
            return set()

        assignments: dict[str, int] = {}
        for match in re.finditer(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(\d{2,5})\b", text, re.M):
            name = match.group(1)
            if "port" in name.lower():
                assignments[name] = int(match.group(2))

        ports: set[int] = set()
        for match in re.finditer(r"\bport\s*=\s*(\d{2,5})\b", text, re.I):
            ports.add(int(match.group(1)))
        for match in re.finditer(r"\bport\s*=\s*([A-Za-z_][A-Za-z0-9_]*)\b", text, re.I):
            value = assignments.get(match.group(1))
            if value:
                ports.add(value)
        return {port for port in ports if 1 <= port <= 65535}

    def _port_listener(self, port: int) -> dict | None:
        if port <= 0 or port > 65535:
            return None
        connected_host = ""
        for host in ["127.0.0.1", "::1"]:
            try:
                with socket.create_connection((host, port), timeout=0.2):
                    connected_host = host
                    break
            except OSError:
                continue
        if not connected_host:
            return None

        listener = {"port": port, "host": connected_host}
        listener.update(self._port_owner(port))
        return listener

    def _port_owner(self, port: int) -> dict:
        if os.name != "nt":
            return {}
        command = (
            "$c=Get-NetTCPConnection -LocalPort "
            f"{port} -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1; "
            "if ($c) { "
            "$p=Get-Process -Id $c.OwningProcess -ErrorAction SilentlyContinue; "
            "[pscustomobject]@{pid=$c.OwningProcess;process=$p.ProcessName;path=$p.Path;address=$c.LocalAddress} | ConvertTo-Json -Compress "
            "}"
        )
        try:
            result = subprocess.run(
                ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", command],
                capture_output=True,
                text=True,
                timeout=3,
            )
            raw = (result.stdout or "").strip()
            if result.returncode == 0 and raw:
                data = json.loads(raw)
                return {key: value for key, value in data.items() if value not in (None, "")}
        except Exception:
            return {}
        return {}

    def _can_bind_port(self, port: int) -> bool:
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
                sock.bind(("127.0.0.1", port))
            return True
        except OSError:
            return False

    def _port_conflict_text(self, listener: dict) -> str:
        port = listener.get("port")
        pid = listener.get("pid")
        process = listener.get("process")
        owner = f" by pid {pid}" if pid else ""
        if process:
            owner += f" ({process})"
        return f"Port {port} is already in use{owner}. Choose a free port or stop the existing listener."
