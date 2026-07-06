from __future__ import annotations

import json
import shutil  # noqa: F401 - tests monkeypatch neo.services.tools.shutil.which
from pathlib import Path
from typing import Any, Dict

from ..config import ARTIFACTS_DIR, Settings  # noqa: F401 - ARTIFACTS_DIR is monkeypatched in tests
from ..tools.base import ToolResult, ToolboxHelpers
from ..tools.computer_tools import ComputerTools
from ..tools.coordination import CoordinationTools
from ..tools.external import ExternalTools
from ..tools.files import FileTools
from ..tools.media import MediaTools
from ..tools.quality import QualityTools
from ..tools.registry import describe_tools
from ..tools.runtime_tools import RuntimeTools
from ..tools.security import SecurityTools
from ..tools.setup_tools import SetupTools
from ..tools.shell import ShellTools
from ..tools.web import ReadablePageParser, WebTools
from . import computer  # noqa: F401 - kept for tests and callers that patch neo.services.tools-level access
from . import computer_access
from .process_manager import ProcessManager
from .runtime import get_workspace_dir


__all__ = ["Toolbox", "ToolResult", "ReadablePageParser"]


class Toolbox(
    FileTools,
    MediaTools,
    ShellTools,
    RuntimeTools,
    SetupTools,
    ComputerTools,
    QualityTools,
    SecurityTools,
    WebTools,
    ExternalTools,
    CoordinationTools,
    ToolboxHelpers,
):
    """Composition point for all tool domains.

    The domain behavior lives in neo/tools/*; this class owns the workspace
    boundary, the tool catalog, strict argument validation, and dispatch.
    """

    def __init__(self, workspace: Path | None = None) -> None:
        self._workspace_override = workspace
        self.workspace.mkdir(parents=True, exist_ok=True)
        self.processes = ProcessManager()

    @property
    def workspace(self) -> Path:
        return (self._workspace_override or get_workspace_dir()).resolve()

    def _artifacts_root(self) -> Path:
        # Resolved at call time from this module so tests can monkeypatch
        # neo.services.tools.ARTIFACTS_DIR.
        return ARTIFACTS_DIR

    def describe(self) -> list[dict]:
        return describe_tools()

    @property
    def _arg_specs(self) -> Dict[str, set]:
        cached = getattr(self, "_arg_specs_cache", None)
        if cached is None:
            cached = {str(item["name"]): set((item.get("args") or {}).keys()) for item in self.describe()}
            self._arg_specs_cache = cached
        return cached

    def validate_args(self, tool: str, args: Dict[str, Any]) -> ToolResult | None:
        """Reject unknown arguments instead of silently dropping them.

        Silently ignored args have caused real failures (a start_process call
        with an ignored 'path' launched flask from the wrong directory).
        """
        allowed = self._arg_specs.get(tool)
        if allowed is None:
            return None
        unknown = sorted(set(args.keys()) - allowed)
        if not unknown:
            return None
        allowed_text = ", ".join(sorted(allowed)) or "(no arguments)"
        return ToolResult(
            False,
            tool,
            f"Unknown argument(s) for {tool}: {', '.join(unknown)}. Allowed arguments: {allowed_text}. "
            "Retry the call using only allowed arguments.",
            {"invalid_args": unknown, "allowed_args": sorted(allowed)},
        )

    def execute(self, tool: str, args: Dict[str, Any]) -> ToolResult:
        try:
            args = args if isinstance(args, dict) else {}
            invalid = self.validate_args(tool, args)
            if invalid:
                return invalid
            if tool in computer_access.GATED_TOOLS and not computer_access.allowed():
                return ToolResult(False, tool, computer_access.denial_message(tool), {
                    "approval_required": True,
                    "access_mode": computer_access.mode(),
                })
            if tool == "powershell":
                return self._powershell(str(args.get("command", "")))
            if tool == "wsl_status":
                return self._wsl_status()
            if tool == "wsl_probe":
                return self._wsl_probe(str(args.get("distro", "")))
            if tool == "wsl":
                return self._wsl(str(args.get("command", "")), str(args.get("distro", "")))
            if tool == "project_probe":
                return self._project_probe(str(args.get("path", ".")))
            if tool == "port_check":
                return self._port_check(int(args.get("port", 0) or 0))
            if tool == "find_free_port":
                return self._find_free_port(int(args.get("start", 3000) or 3000), int(args.get("end", 9000) or 9000))
            if tool == "tool_versions":
                return self._tool_versions()
            if tool == "python_venv":
                return self._python_venv(
                    str(args.get("venv_path", ".neo/venv")),
                    str(args.get("requirements", "requirements.txt")),
                    self._flag(args.get("install", False)),
                )
            if tool == "python_install":
                return self._python_install(
                    str(args.get("path", ".")),
                    str(args.get("venv_path", ".neo/venv")),
                    str(args.get("requirements", "requirements.txt")),
                    self._flag(args.get("create_venv", True)),
                )
            if tool == "node_install":
                return self._node_install(
                    str(args.get("path", ".")),
                    str(args.get("package_manager", "auto")),
                    self._flag(args.get("frozen", True)),
                    self._flag(args.get("production", False)),
                    self._flag(args.get("force", False)),
                )
            if tool == "start_process":
                return self._start_process(
                    str(args.get("command", "")),
                    str(args.get("name", "process")),
                    str(args.get("path", "")),
                    str(args.get("venv_path", "")),
                    int(args.get("port", 0) or 0),
                    float(args.get("wait_seconds", 0) or 0),
                )
            if tool == "process_status":
                return self._process_status(
                    int(args.get("pid", 0) or 0),
                    str(args.get("stdout_log", "")),
                    str(args.get("stderr_log", "")),
                )
            if tool == "stop_process":
                return self._stop_process(int(args.get("pid", 0) or 0))
            if tool == "list_processes":
                return self._list_processes()
            if tool == "screen_capture":
                # Default to the PRIMARY monitor (1), never mss index 0 (the
                # stitched all-screens box), so grid coords map to real clicks.
                return self._screen_capture(int(args.get("monitor", 1) or 1))
            if tool == "computer_click":
                return self._computer_action("computer_click", lambda: computer.click(
                    int(args.get("x", 0) or 0),
                    int(args.get("y", 0) or 0),
                    str(args.get("button", "left") or "left"),
                    self._flag(args.get("double", False)),
                ))
            if tool == "computer_move":
                return self._computer_action("computer_move", lambda: computer.move(
                    int(args.get("x", 0) or 0),
                    int(args.get("y", 0) or 0),
                ))
            if tool == "computer_type":
                return self._computer_action("computer_type", lambda: computer.type_text(str(args.get("text", ""))))
            if tool == "computer_type_at":
                return self._computer_action("computer_type_at", lambda: computer.click_and_type(
                    int(args.get("x", 0) or 0),
                    int(args.get("y", 0) or 0),
                    str(args.get("text", "")),
                    self._flag(args.get("clear", False)),
                    self._flag(args.get("submit", False)),
                ))
            if tool == "open_app":
                return self._open_app(str(args.get("name", "")))
            if tool == "computer_key":
                return self._computer_action("computer_key", lambda: computer.press_keys(str(args.get("keys", ""))))
            if tool == "computer_scroll":
                x = args.get("x")
                y = args.get("y")
                return self._computer_action("computer_scroll", lambda: computer.scroll(
                    int(args.get("amount", 0) or 0),
                    int(x) if x is not None else None,
                    int(y) if y is not None else None,
                ))
            if tool == "list_windows":
                return self._list_windows()
            if tool == "focus_window":
                return self._computer_action("focus_window", lambda: computer.focus_window(str(args.get("title", ""))))
            if tool == "python":
                return self._python(str(args.get("code", "")))
            if tool == "read_file":
                return self._read_file(
                    str(args.get("path", "")),
                    int(args.get("start_line", 0) or 0),
                    int(args.get("line_count", 0) or 0),
                )
            if tool == "write_file":
                return self._write_file(
                    str(args.get("path", "")),
                    str(args.get("content", "")),
                    self._flag(args.get("force", False)),
                )
            if tool == "append_file":
                return self._append_file(str(args.get("path", "")), str(args.get("content", "")))
            if tool == "edit_file":
                return self._edit_file(
                    str(args.get("path", "")),
                    str(args.get("old", "")),
                    str(args.get("new", "")),
                    self._flag(args.get("replace_all", False)),
                    self._flag(args.get("force", False)),
                )
            if tool == "make_dir":
                return self._make_dir(str(args.get("path", "")))
            if tool == "move_path":
                return self._move_path(
                    str(args.get("source", "")),
                    str(args.get("destination", "")),
                    self._flag(args.get("overwrite", False)),
                )
            if tool == "copy_path":
                return self._copy_path(
                    str(args.get("source", "")),
                    str(args.get("destination", "")),
                    self._flag(args.get("recursive", False)),
                )
            if tool == "delete_path":
                return self._delete_path(
                    str(args.get("path", "")),
                    self._flag(args.get("recursive", False)),
                )
            if tool == "file_info":
                return self._file_info(str(args.get("path", ".")))
            if tool == "tree":
                return self._tree(str(args.get("path", ".")), int(args.get("max_depth", 3) or 3))
            if tool == "list_files":
                return self._list_files(str(args.get("path", ".")))
            if tool == "search_files":
                return self._search_files(
                    str(args.get("path", ".")),
                    str(args.get("pattern", "*")),
                    int(args.get("max_results", 100) or 100),
                )
            if tool == "grep":
                return self._grep(
                    str(args.get("path", ".")),
                    str(args.get("pattern", "")),
                    self._flag(args.get("case_sensitive", False)),
                    int(args.get("max_results", 100) or 100),
                )
            if tool == "python_symbols":
                return self._python_symbols(str(args.get("path", "")))
            if tool == "json_validate":
                return self._json_validate(str(args.get("text", "")))
            if tool == "image_info":
                return self._image_info(str(args.get("path", "")))
            if tool == "pdf_info":
                return self._pdf_info(str(args.get("path", "")))
            if tool == "pdf_text_extract":
                return self._pdf_text_extract(
                    str(args.get("path", "")),
                    int(args.get("page_limit", 5) or 5),
                    int(args.get("char_limit", 12000) or 12000),
                )
            if tool == "syntax_check":
                return self._syntax_check(str(args.get("path", ".")), int(args.get("max_files", 80) or 80))
            if tool == "run_tests":
                return self._run_tests(
                    str(args.get("runner", "pytest")),
                    str(args.get("path", ".")),
                    int(args.get("maxfail", 1) or 1),
                    int(args.get("timeout_seconds", 0) or 0),
                )
            if tool == "system_security_audit":
                return self._system_security_audit(str(args.get("scope", "host")))
            if tool == "secrets_scan":
                return self._secrets_scan(str(args.get("path", ".")), int(args.get("max_files", 500) or 500))
            if tool == "dependency_audit":
                return self._dependency_audit(str(args.get("path", ".")), str(args.get("ecosystem", "auto")))
            if tool == "web_search":
                return self._web_search(str(args.get("query", "")))
            if tool == "web_fetch":
                return self._web_fetch(str(args.get("url", "")))
            if tool == "web_links":
                return self._web_links(str(args.get("url", "")))
            if tool == "scrape_page":
                return self._scrape_page(str(args.get("url", "")))
            if tool == "scrape_urls":
                raw_urls = args.get("urls", [])
                urls = raw_urls if isinstance(raw_urls, list) else [str(raw_urls)]
                return self._scrape_urls([str(url) for url in urls])
            if tool == "research_web":
                return self._research_web(str(args.get("query", "")), int(args.get("max_pages", 3) or 3))
            if tool == "http_head":
                return self._http_head(str(args.get("url", "")))
            if tool == "http_get":
                return self._http_get(str(args.get("url", "")))
            if tool == "app_healthcheck":
                raw_checks = args.get("checks", [])
                return self._app_healthcheck(
                    str(args.get("url", "")),
                    str(args.get("path", "")),
                    raw_checks if isinstance(raw_checks, list) else [],
                )
            if tool == "open_browser":
                return self._open_browser(str(args.get("url", "")))
            if tool == "open_vscode":
                return self._open_vscode(str(args.get("path", ".")))
            if tool == "download_url":
                return self._download_url(
                    str(args.get("url", "")),
                    str(args.get("path", "")),
                    int(args.get("max_bytes", 5_000_000) or 5_000_000),
                )
            if tool == "sql_query":
                return self._sql_query(str(args.get("query", "")))
            if tool == "git_status":
                return self._git(["status", "--short"])
            if tool == "git_diff":
                path = str(args.get("path", "")).strip()
                if path:
                    rel = str(self._safe_path(path).relative_to(self.workspace))
                    command = ["diff", "--", rel]
                else:
                    command = ["diff"]
                return self._git(command)
            if tool == "context_read":
                return self._context_read(int(args.get("limit", 20) or 20))
            if tool == "context_write":
                return self._context_write(str(args.get("content", "")), int(args.get("importance", 2) or 2))
            if tool == "list_agents":
                return self._list_agents()
            if tool == "project_brief_set":
                return self._project_brief_set(
                    str(args.get("goal", "")),
                    str(args.get("stack", "")),
                    str(args.get("conventions", "")),
                )
            if tool == "project_task_add":
                return self._project_task_add(
                    str(args.get("title", "")),
                    str(args.get("owner", "")),
                    str(args.get("depends_on", "")),
                    str(args.get("deliverable", "")),
                )
            if tool == "project_task_update":
                return self._project_task_update(
                    int(args.get("task_id", 0) or 0),
                    str(args.get("status", "")),
                    str(args.get("notes", "")),
                )
            if tool == "project_status":
                return self._project_status()
            if tool == "metrics_snapshot":
                return self._metrics_snapshot()
            if tool == "tool_catalog":
                catalog = self.describe()
                return ToolResult(True, "tool_catalog", json.dumps(catalog, indent=2), {"count": len(catalog)})
            if tool == "write_artifact":
                return self._write_artifact(str(args.get("name", "artifact.txt")), str(args.get("content", "")))
            return ToolResult(False, tool, f"Unknown tool: {tool}", {})
        except Exception as exc:
            return ToolResult(False, tool, str(exc), {"error_type": type(exc).__name__})
