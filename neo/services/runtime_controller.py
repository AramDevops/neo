from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Dict, List

from .plan_engine import PlanEngine
from .tools import Toolbox


class RuntimeController:
    """Deterministic control plane for app/runtime work.

    The model may request tools, but this controller decides the safe order for
    runtime work: probe, install, reserve/patch a port, start, then verify.
    """

    def __init__(self, plan_engine: PlanEngine, toolbox: Toolbox) -> None:
        self.plan_engine = plan_engine
        self.toolbox = toolbox

    def required_tool_calls(
        self,
        user_message: str,
        observations: List[Dict[str, Any]],
        requested_calls: List[Any],
        plan: List[Any] | None = None,
    ) -> List[Any]:
        calls = self.plan_engine.required_tool_calls(user_message, observations, requested_calls)
        return self.runtime_required_tool_calls(user_message, observations, calls, plan)

    def continue_without_tools_reason(
        self,
        user_message: str,
        plan: List[Any],
        final_text: str,
        observations: List[Dict[str, Any]],
    ) -> str:
        if not self.plan_engine.goal_requires_work(user_message):
            return ""
        if self.plan_engine.organization_required(user_message) and not self.plan_engine.organization_satisfied(observations, final_text):
            return (
                "Neo policy retry: the user intent is an actionable organization request. "
                "Infer typo-heavy wording from context, do not ask for clarification yet, and continue with inspect -> move/group -> verify using available tools. "
                "If no move is needed, verify the tree and state why nothing needed moving."
            )
        if self.plan_engine.security_audit_required(user_message):
            completed = {str(item.get("tool") or "") for item in observations if item.get("ok")}
            missing = [tool for tool in ["system_security_audit", "secrets_scan", "dependency_audit"] if tool not in completed]
            if missing:
                return "Neo policy retry: run the required bounded security diagnostics before asking for clarification. Missing tools: " + ", ".join(missing)
        if self.plan_engine.browser_required(user_message, plan, final_text):
            completed = {str(item.get("tool") or "") for item in observations if item.get("ok")}
            if not ({"start_process", "http_get", "http_head", "open_browser"} & completed):
                return (
                    "Neo policy retry: this is an actionable app-run request. "
                    "Do not finish yet. Inspect the project if needed, start the app with start_process on an available port, "
                    "then verify with http_get and open_browser. If dependencies are missing, install them with the appropriate structured install tool."
                )
        final_lower = (final_text or "").lower()
        if any(phrase in final_lower for phrase in ["could you clarify", "please clarify", "what do you mean", "need more information"]):
            return "Neo policy retry: the user asked for work. Use the available tools to inspect and act before asking for clarification."
        return ""

    def runtime_required_tool_calls(
        self,
        user_message: str,
        observations: List[Dict[str, Any]],
        calls: List[Any],
        plan: List[Any] | None = None,
    ) -> List[Any]:
        normalized_calls = [call for call in calls if isinstance(call, dict)]
        if (
            self.plan_engine.is_status_question(user_message)
            or self.plan_engine.is_web_action(user_message)
            or self.plan_engine.is_desktop_gui_task(user_message)
        ):
            # Status questions, web-search / open-website requests, and desktop
            # GUI control ("open calculator and click the buttons") are answered
            # with observation, the web tools, or computer tools; never escalate
            # them into project probes, installs, port changes, or process
            # starts. (A plain "search Google for X" was hijacked into starting
            # a leftover local app on port 3000; "open calculator" was dragged
            # into node_install/start_process on unrelated leftover projects.)
            return normalized_calls
        queued = {str(call.get("tool") or "") for call in normalized_calls}
        # The stored plan is part of the intent signal: browser_required must
        # see the SAME context here as at verdict time, or a run whose plan
        # says "Start the app / Open the browser" is judged on steps the
        # controller never drove (run 65: typo-heavy create-app message ended
        # blocked with zero probe/install/start injections).
        runtime_context = self.plan_engine.browser_required(user_message, plan or [], "") or bool(self._successful_project_probes(observations))
        if not runtime_context:
            return normalized_calls

        # The project this run is actually building (files it created/edited)
        # wins over workspace-wide discovery. Without this, a calculator task
        # got hijacked: the controller found the unrelated calendar backend,
        # started it, and opened its API route.
        active_dirs = self._active_project_dirs(observations)
        probes = self._successful_project_probes(observations)
        if active_dirs:
            probes = [
                probe for probe in probes
                if self._clean_rel_path(probe.get("relative_path", ".")).split("/")[0] in active_dirs
            ]
        probes = self._target_probes(user_message, probes)
        if not probes:
            injected: list[dict[str, Any]] = []
            for path in self._app_project_candidates(user_message)[:2]:
                if active_dirs and self._clean_rel_path(path).split("/")[0] not in active_dirs:
                    continue
                if not self._probe_seen_or_queued(path, observations, normalized_calls):
                    injected.append({"tool": "project_probe", "args": {"path": path}})
            if injected:
                return [*injected, *self._defer_runtime_calls(normalized_calls)]
            if active_dirs:
                return normalized_calls
            if not self._probe_seen_or_queued(".", observations, normalized_calls):
                return [{"tool": "project_probe", "args": {"path": "."}}, *self._defer_runtime_calls(normalized_calls)]
            return normalized_calls

        for probe in probes:
            install_call = self._runtime_install_call(probe, observations, normalized_calls)
            if install_call:
                return [install_call, *self._defer_runtime_calls(normalized_calls)]

        for probe in probes:
            port_call = self._runtime_python_port_prepare_call(probe, observations, normalized_calls)
            if port_call:
                return [port_call, *self._defer_runtime_calls(normalized_calls)]

        if self._failed_port_conflict_seen(observations) and not self._observed_free_port(observations):
            if not any(call.get("tool") == "find_free_port" for call in normalized_calls):
                return [{"tool": "find_free_port", "args": {"start": 8000, "end": 8999}}, *self._defer_runtime_calls(normalized_calls)]

        port_retry_call = self._runtime_port_retry_start_call(observations, normalized_calls)
        if port_retry_call:
            return [port_retry_call, *self._defer_runtime_calls(normalized_calls)]

        # Check-before-act for starts: when this project's server is ALREADY
        # running (live managed process with a listening port), verify it over
        # HTTP instead of starting a duplicate: run 106 started a second
        # instance and crashed EADDRINUSE against the first one.
        verify_call = self._running_app_verify_call(probes, observations, normalized_calls)
        if verify_call:
            return [verify_call, *self._defer_runtime_calls(normalized_calls)]

        if queued & {"start_process", "http_get", "http_head", "open_browser"}:
            return normalized_calls

        for probe in probes:
            start_call = self._runtime_start_call(probe, observations, normalized_calls)
            if start_call:
                return [start_call, *normalized_calls]
        return normalized_calls

    def _running_app_verify_call(
        self,
        probes: List[Dict[str, Any]],
        observations: List[Dict[str, Any]],
        calls: List[Dict[str, Any]],
    ) -> Dict[str, Any] | None:
        for probe in probes:
            rel = self._clean_rel_path(probe.get("relative_path", "."))
            entry = self._running_managed_entry(rel)
            if not entry:
                continue
            port = entry.get("listening_port") or next((p for p in entry.get("ports") or [] if p), 0)
            try:
                port = int(port)
            except (TypeError, ValueError):
                port = 0
            if not 1 <= port <= 65535:
                continue
            url = f"http://127.0.0.1:{port}"
            if self._url_checked_or_queued(url, observations, calls):
                continue
            return {"tool": "http_get", "args": {"url": url}}
        return None

    def _running_managed_entry(self, rel: str) -> Dict[str, Any] | None:
        clean = self._clean_rel_path(rel).replace("\\", "/")
        if clean == ".":
            return None
        try:
            entries = self.toolbox.processes.list_managed()
        except Exception:
            return None
        for entry in entries:
            if not entry.get("running"):
                continue
            haystack = " ".join([
                str(entry.get("command") or ""),
                str(entry.get("cwd") or ""),
                str(entry.get("name") or ""),
            ]).replace("\\", "/")
            if clean in haystack:
                return entry
        return None

    def _url_checked_or_queued(
        self,
        url: str,
        observations: List[Dict[str, Any]],
        calls: List[Dict[str, Any]],
    ) -> bool:
        clean = url.rstrip("/")
        for observation in observations:
            if observation.get("tool") not in {"http_get", "http_head"}:
                continue
            args = observation.get("args") if isinstance(observation.get("args"), dict) else {}
            if str(args.get("url") or "").rstrip("/") == clean:
                return True
        for call in calls:
            if call.get("tool") not in {"http_get", "http_head"}:
                continue
            args = call.get("args") if isinstance(call.get("args"), dict) else {}
            if str(args.get("url") or "").rstrip("/") == clean:
                return True
        return False

    def _target_probes(self, user_message: str, probes: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """When the message identifies a specific project, drive ONLY that one.
        Run 102 ("run the real time watch") probed both workspace projects and
        the controller then installed/started text-editor-app too. Run 110: an
        auto-recovery follow-up ("app wont run its broken: ...:3003/style.css")
        dropped the project NAME, so token scoring hit 0 for every project and
        the controller drove them all again, but the message still carried the
        port (:3003) that pins the project via the process registry."""
        if len(probes) <= 1:
            return probes

        message_ports = self._message_ports(user_message)
        scored = [(self._probe_target_score(user_message, message_ports, probe), probe) for probe in probes]
        best = max(value for value, _ in scored)
        if best <= 0:
            return probes
        return [probe for value, probe in scored if value == best]

    def _probe_target_score(self, user_message: str, message_ports: set[int], probe: Dict[str, Any]) -> int:
        rel = self._clean_rel_path(probe.get("relative_path", ".")).lower()
        value = 0
        for token in re.findall(r"[a-z0-9]+", user_message.lower()):
            if len(token) >= 4 and token in rel:
                value += 1
        # Port lock: a port in the message that a managed process for THIS
        # project served pins the target even when a follow-up dropped the name.
        if message_ports and self._project_has_managed_port(rel, message_ports):
            value += 5
        return value

    def _message_ports(self, text: str) -> set[int]:
        ports: set[int] = set()
        for match in re.finditer(r"(?::|port\s+)(\d{2,5})\b", (text or "").lower()):
            try:
                port = int(match.group(1))
            except ValueError:
                continue
            if 1 <= port <= 65535:
                ports.add(port)
        return ports

    def _project_has_managed_port(self, rel: str, ports: set[int]) -> bool:
        """True when a managed process (running or persisted/orphaned) whose
        cwd/command/name references this project served one of these ports."""
        clean = self._clean_rel_path(rel).replace("\\", "/")
        if clean == "." or not ports:
            return False
        try:
            entries = self.toolbox.processes.list_managed()
        except Exception:
            return False
        for entry in entries:
            haystack = " ".join([
                str(entry.get("cwd") or ""),
                str(entry.get("command") or ""),
                str(entry.get("name") or ""),
            ]).replace("\\", "/")
            if clean not in haystack:
                continue
            entry_ports: set[int] = set()
            for raw in list(entry.get("ports") or []) + [entry.get("listening_port")]:
                try:
                    if raw is not None:
                        entry_ports.add(int(raw))
                except (TypeError, ValueError):
                    continue
            if entry_ports & ports:
                return True
        return False

    def _defer_runtime_calls(self, calls: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        blocked = {"start_process", "http_get", "http_head", "open_browser"}
        return [call for call in calls if str(call.get("tool") or "") not in blocked]

    def _active_project_dirs(self, observations: List[Dict[str, Any]]) -> set[str]:
        """Top-level workspace dirs this run actually touched (created/edited/
        moved files in). Used to keep runtime work on THIS run's project rather
        than an unrelated project discovered elsewhere in the workspace."""
        write_tools = {"write_file", "append_file", "edit_file", "make_dir", "move_path", "download_url"}
        dirs: set[str] = set()
        for observation in observations:
            if observation.get("tool") not in write_tools or not observation.get("ok"):
                continue
            args = observation.get("args") if isinstance(observation.get("args"), dict) else {}
            meta = observation.get("meta") if isinstance(observation.get("meta"), dict) else {}
            for raw in (meta.get("relative_path"), args.get("path"), args.get("destination")):
                clean = self._clean_rel_path(raw)
                if clean and clean != ".":
                    dirs.add(clean.split("/")[0])
        return dirs

    def _successful_project_probes(self, observations: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        probes: list[dict[str, Any]] = []
        for observation in observations:
            if not observation.get("ok") or observation.get("tool") != "project_probe":
                continue
            meta = observation.get("meta") if isinstance(observation.get("meta"), dict) else {}
            if meta.get("python_project") or meta.get("node_project"):
                probes.append(meta)
        return probes

    def _app_project_candidates(self, user_message: str) -> List[str]:
        workspace = self.toolbox.workspace
        skip_dirs = {".git", ".pytest_cache", ".neo", "node_modules", "__pycache__", "venv", ".venv", "dist", "build"}
        markers = {"app.py", "wsgi.py", "manage.py", "package.json", "pyproject.toml", "requirements.txt"}
        candidates: list[Path] = []
        stack: list[tuple[Path, int]] = [(workspace, 0)]
        while stack:
            root, depth = stack.pop(0)
            try:
                children = list(root.iterdir())
            except OSError:
                continue
            names = {child.name for child in children if child.is_file()}
            if names & markers:
                candidates.append(root)
            if depth >= 4:
                continue
            for child in sorted((item for item in children if item.is_dir()), key=lambda item: item.name.lower()):
                if child.name in skip_dirs:
                    continue
                stack.append((child, depth + 1))

        def score(path: Path) -> tuple[int, str]:
            rel = "." if path == workspace else str(path.relative_to(workspace))
            lowered = rel.lower()
            message = user_message.lower()
            value = 0
            for token in re.findall(r"[a-z0-9]+", message):
                if len(token) >= 4 and token in lowered:
                    value += 6
            if "calendar" in message and "calendar" in lowered:
                value += 12
            if "frontend" in message and "frontend" in lowered:
                value += 8
            if "backend" in message and "backend" in lowered:
                value += 8
            if lowered.endswith("frontend") or lowered.endswith("backend"):
                value += 2
            return (-value, rel)

        ordered = sorted(dict.fromkeys(candidates), key=score)
        return [
            "." if path == workspace else str(path.relative_to(workspace)).replace("\\", "/")
            for path in ordered
        ]

    def _probe_seen_or_queued(
        self,
        path: str,
        observations: List[Dict[str, Any]],
        calls: List[Dict[str, Any]],
    ) -> bool:
        clean = self._clean_rel_path(path)
        for observation in observations:
            if observation.get("tool") != "project_probe":
                continue
            args = observation.get("args") if isinstance(observation.get("args"), dict) else {}
            meta = observation.get("meta") if isinstance(observation.get("meta"), dict) else {}
            seen_paths = {
                self._clean_rel_path(args.get("path", "")),
                self._clean_rel_path(meta.get("relative_path", "")),
            }
            if clean in seen_paths:
                return True
        for call in calls:
            if call.get("tool") != "project_probe":
                continue
            args = call.get("args") if isinstance(call.get("args"), dict) else {}
            if clean == self._clean_rel_path(args.get("path", "")):
                return True
        return False

    def _runtime_install_call(
        self,
        probe: Dict[str, Any],
        observations: List[Dict[str, Any]],
        calls: List[Dict[str, Any]],
    ) -> Dict[str, Any] | None:
        rel = self._clean_rel_path(probe.get("relative_path", "."))
        common = probe.get("common_files") if isinstance(probe.get("common_files"), dict) else {}
        if (
            probe.get("node_project")
            and common.get("package.json")
            # Check-before-act: the probe already read the install state; a
            # project whose declared dependencies are present needs no install
            # (previously every run re-ran npm install unconditionally).
            and not probe.get("node_dependencies_installed")
            and not self._tool_attempted_for_path("node_install", rel, observations, calls)
        ):
            return {"tool": "node_install", "args": {"path": rel, "package_manager": "auto", "frozen": True, "production": False}}

        python_iso = probe.get("python_isolation") if isinstance(probe.get("python_isolation"), dict) else {}
        requirements = str(python_iso.get("requirements") or ("requirements.txt" if common.get("requirements.txt") else "")).strip()
        if probe.get("python_project") and requirements and not self._tool_attempted_for_path("python_install", rel, observations, calls):
            return {"tool": "python_install", "args": {"path": rel, "venv_path": ".neo/venv", "requirements": requirements, "create_venv": True}}
        return None

    def _runtime_start_call(
        self,
        probe: Dict[str, Any],
        observations: List[Dict[str, Any]],
        calls: List[Dict[str, Any]],
    ) -> Dict[str, Any] | None:
        rel = self._clean_rel_path(probe.get("relative_path", "."))
        if self._tool_attempted_for_path("start_process", rel, observations, calls):
            return None
        if self._running_managed_entry(rel):
            # Already serving: never inject a duplicate start.
            return None
        common = probe.get("common_files") if isinstance(probe.get("common_files"), dict) else {}
        scripts = probe.get("package_scripts") if isinstance(probe.get("package_scripts"), dict) else {}

        if probe.get("node_project") and common.get("package.json"):
            script = "dev" if "dev" in scripts else ("start" if "start" in scripts else "")
            if script:
                return {
                    "tool": "start_process",
                    "args": {
                        "command": f"Set-Location {self._ps_quote(rel)}; npm run {script} -- --host 127.0.0.1",
                        "name": f"{Path(rel).name or 'frontend'}_{script}",
                    },
                }

        if probe.get("python_project"):
            script_name = "manage.py" if common.get("manage.py") else ("app.py" if common.get("app.py") else "")
            if script_name:
                script_path = script_name if rel == "." else f"{rel}/{script_name}"
                if script_name == "manage.py":
                    command = f"python {self._ps_quote(script_path)} runserver 127.0.0.1:8000"
                else:
                    # PowerShell needs the call operator to run a quoted command word;
                    # without it, 'python' 'app.py' is a parse error and the child
                    # dies before the readiness gate can observe anything useful.
                    command = f"& {self._ps_quote(self._python_for_project(rel, observations))} {self._ps_quote(script_path)}"
                return {"tool": "start_process", "args": {"command": command, "name": f"{Path(rel).name or 'python'}_app"}}
        return None

    def _runtime_python_port_prepare_call(
        self,
        probe: Dict[str, Any],
        observations: List[Dict[str, Any]],
        calls: List[Dict[str, Any]],
    ) -> Dict[str, Any] | None:
        rel = self._clean_rel_path(probe.get("relative_path", "."))
        common = probe.get("common_files") if isinstance(probe.get("common_files"), dict) else {}
        if not probe.get("python_project") or not common.get("app.py"):
            return None
        if self._successful_start_for_path(rel, observations):
            return None

        free_port = self._observed_free_port(observations)
        if not free_port:
            if not any(call.get("tool") == "find_free_port" for call in calls):
                return {"tool": "find_free_port", "args": {"start": 8000, "end": 8999}}
            return None

        app_path = self._workspace_project_file(rel, "app.py")
        if not app_path or not app_path.exists():
            return None
        text = app_path.read_text(encoding="utf-8", errors="replace")
        edit_args = self._app_run_port_edit_args(rel, text, free_port)
        if not edit_args:
            return None
        if self._same_edit_seen_or_queued(edit_args, observations, calls):
            return None
        return {"tool": "edit_file", "args": edit_args}

    def _successful_start_for_path(self, rel: str, observations: List[Dict[str, Any]]) -> bool:
        clean = self._clean_rel_path(rel).replace("\\", "/")
        stopped_pids = set()
        for observation in observations:
            if observation.get("tool") != "stop_process" or not observation.get("ok"):
                continue
            args = observation.get("args") if isinstance(observation.get("args"), dict) else {}
            meta = observation.get("meta") if isinstance(observation.get("meta"), dict) else {}
            for raw in (args.get("pid"), meta.get("pid")):
                try:
                    stopped_pids.add(int(raw))
                except (TypeError, ValueError):
                    continue
        for observation in observations:
            if observation.get("tool") != "start_process" or not observation.get("ok"):
                continue
            args = observation.get("args") if isinstance(observation.get("args"), dict) else {}
            meta = observation.get("meta") if isinstance(observation.get("meta"), dict) else {}
            try:
                pid = int(meta.get("pid") or 0)
            except (TypeError, ValueError):
                pid = 0
            if pid and pid in stopped_pids:
                # A start that was later stopped is history, not current state:
                # the controller must stay engaged for the restart.
                continue
            haystack = " ".join([str(args.get("command") or ""), str(meta.get("command") or "")]).replace("\\", "/")
            if clean == "." or clean in haystack:
                return True
        return False

    def _observed_free_port(self, observations: List[Dict[str, Any]]) -> int:
        for observation in reversed(observations):
            if observation.get("tool") != "find_free_port" or not observation.get("ok"):
                continue
            meta = observation.get("meta") if isinstance(observation.get("meta"), dict) else {}
            try:
                port = int(meta.get("port") or 0)
            except Exception:
                port = 0
            if 1 <= port <= 65535:
                return port
        return 0

    def _failed_port_conflict_seen(self, observations: List[Dict[str, Any]]) -> bool:
        for observation in observations:
            if observation.get("tool") != "start_process" or observation.get("ok"):
                continue
            meta = observation.get("meta") if isinstance(observation.get("meta"), dict) else {}
            if meta.get("port_conflicts"):
                return True
        return False

    def _workspace_project_file(self, rel: str, filename: str) -> Path | None:
        try:
            root = (self.toolbox.workspace / ("" if rel == "." else rel)).resolve()
            target = (root / filename).resolve()
            workspace = self.toolbox.workspace.resolve()
            if target == workspace or workspace not in target.parents:
                return None
            return target
        except Exception:
            return None

    def _app_run_port_edit_args(self, rel: str, text: str, port: int) -> Dict[str, Any] | None:
        match = re.search(r"\bapp\.run\(([^)\n]*)\)", text)
        if not match:
            return None
        old = match.group(0)
        inner = match.group(1).strip()
        if re.search(rf"\bport\s*=\s*{port}\b", inner):
            return None
        if re.search(r"\bport\s*=\s*\d{1,5}\b", inner):
            new_inner = re.sub(r"\bport\s*=\s*\d{1,5}\b", f"port={port}", inner, count=1)
        else:
            new_inner = f"{inner}, port={port}" if inner else f"port={port}"
        return {
            "path": f"{rel}/app.py" if rel != "." else "app.py",
            "old": old,
            "new": f"app.run({new_inner})",
            # replace_all so a duplicated app.run(...) line (e.g. a commented
            # copy) does not trip edit_file's exact-match-once guard and dead-
            # end port recovery; every identical launch line targets the free
            # port, which is safe.
            "replace_all": True,
        }

    def _same_edit_seen_or_queued(
        self,
        edit_args: Dict[str, Any],
        observations: List[Dict[str, Any]],
        calls: List[Dict[str, Any]],
    ) -> bool:
        path = self._clean_rel_path(edit_args.get("path"))
        new = str(edit_args.get("new") or "")
        for observation in observations:
            if observation.get("tool") != "edit_file" or not observation.get("ok"):
                # A FAILED edit must not suppress re-injection, or one rejected
                # port patch permanently disables port-conflict recovery.
                continue
            args = observation.get("args") if isinstance(observation.get("args"), dict) else {}
            if self._clean_rel_path(args.get("path")) == path and str(args.get("new") or "") == new:
                return True
        for call in calls:
            if call.get("tool") != "edit_file":
                continue
            args = call.get("args") if isinstance(call.get("args"), dict) else {}
            if self._clean_rel_path(args.get("path")) == path and str(args.get("new") or "") == new:
                return True
        return False

    def _python_for_project(self, rel: str, observations: List[Dict[str, Any]]) -> str:
        clean = self._clean_rel_path(rel)
        for observation in reversed(observations):
            if observation.get("tool") != "python_install" or not observation.get("ok"):
                continue
            args = observation.get("args") if isinstance(observation.get("args"), dict) else {}
            meta = observation.get("meta") if isinstance(observation.get("meta"), dict) else {}
            path = self._clean_rel_path(meta.get("relative_path") or args.get("path"))
            python = str(meta.get("python") or "").strip()
            if python and (path == clean or path in clean or clean in path):
                return python
        return "python"

    def _runtime_port_retry_start_call(
        self,
        observations: List[Dict[str, Any]],
        calls: List[Dict[str, Any]],
    ) -> Dict[str, Any] | None:
        failed_start = None
        failed_index = -1
        for index, observation in enumerate(observations):
            if observation.get("tool") == "start_process" and not observation.get("ok"):
                meta = observation.get("meta") if isinstance(observation.get("meta"), dict) else {}
                if meta.get("port_conflicts"):
                    failed_start = observation
                    failed_index = index
        if not failed_start:
            return None
        if any(item.get("tool") == "start_process" and item.get("ok") for item in observations[failed_index + 1:]):
            return None

        free_port = 0
        for observation in observations:
            if observation.get("tool") != "find_free_port" or not observation.get("ok"):
                continue
            meta = observation.get("meta") if isinstance(observation.get("meta"), dict) else {}
            try:
                free_port = int(meta.get("port") or 0)
            except Exception:
                free_port = 0
        if not free_port:
            return None

        failed_args = failed_start.get("args") if isinstance(failed_start.get("args"), dict) else {}
        command = str(failed_args.get("command") or "")
        if not command or f":{free_port}" in command or f" {free_port}" in command:
            return None
        if any(call.get("tool") == "start_process" and str((call.get("args") or {}).get("command") or "").find(str(free_port)) >= 0 for call in calls):
            return None

        patched = self._command_with_port(command, free_port)
        if patched == command:
            # The port lives in the script file, not the command. Retry the
            # same command only after a file edit moved the app onto the free
            # port, and only once per edit.
            edit_index = -1
            for index, item in enumerate(observations):
                if index <= failed_index:
                    continue
                if item.get("tool") != "edit_file" or not item.get("ok"):
                    continue
                item_args = item.get("args") if isinstance(item.get("args"), dict) else {}
                if f"port={free_port}" in str(item_args.get("new") or ""):
                    edit_index = index
            if edit_index < 0:
                return None
            if any(item.get("tool") == "start_process" for item in observations[edit_index + 1:]):
                return None
            if any(call.get("tool") == "start_process" for call in calls):
                return None
        return {"tool": "start_process", "args": {"command": patched, "name": str(failed_args.get("name") or "app")}}

    def _command_with_port(self, command: str, port: int) -> str:
        patched = re.sub(r"(--port(?:=|\s+))\d{1,5}", rf"\g<1>{port}", command, flags=re.I)
        patched = re.sub(r"(-p\s+)\d{1,5}", rf"\g<1>{port}", patched, flags=re.I)
        patched = re.sub(r"(runserver\s+(?:127\.0\.0\.1|localhost|0\.0\.0\.0):)\d{1,5}", rf"\g<1>{port}", patched, flags=re.I)
        if re.search(r"\bflask\s+run\b", patched, re.I) and not re.search(r"--port(?:=|\s+)\d{1,5}", patched, re.I):
            return f"{patched} --port {port}"
        if re.search(r"\bnpm\s+run\s+dev\b|\byarn\s+dev\b|\bpnpm\s+dev\b|\bvite\b", patched, re.I) and not re.search(r"--port(?:=|\s+)\d{1,5}", patched, re.I):
            return f"{patched} -- --port {port}"
        return patched

    def _tool_attempted_for_path(
        self,
        tool_name: str,
        rel_path: str,
        observations: List[Dict[str, Any]],
        calls: List[Dict[str, Any]],
    ) -> bool:
        clean = self._clean_rel_path(rel_path).replace("\\", "/")

        def matches(payload: Dict[str, Any]) -> bool:
            args = payload.get("args") if isinstance(payload.get("args"), dict) else {}
            meta = payload.get("meta") if isinstance(payload.get("meta"), dict) else {}
            haystack = " ".join([
                str(args.get("path") or ""),
                str(args.get("command") or ""),
                str(meta.get("relative_path") or ""),
                str(meta.get("command") or ""),
            ]).replace("\\", "/")
            return clean == "." or clean in haystack

        for observation in observations:
            if observation.get("tool") == tool_name and matches(observation):
                return True
        for call in calls:
            if call.get("tool") == tool_name and matches(call):
                return True
        return False

    def _clean_rel_path(self, path: Any) -> str:
        clean = str(path or ".").strip().replace("\\", "/").strip("/")
        return clean or "."

    def _ps_quote(self, value: Any) -> str:
        return "'" + str(value).replace("'", "''") + "'"
