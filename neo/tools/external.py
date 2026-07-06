from __future__ import annotations

import os
import shutil
import subprocess
import urllib.parse
import webbrowser
from pathlib import Path

from .base import ToolResult, ToolboxHelpers


class ExternalTools(ToolboxHelpers):
    """Hand-offs to user-facing applications: browser tabs and VS Code."""

    def _open_browser(self, url: str) -> ToolResult:
        url = url.strip()
        self._validate_url(url)
        parsed = urllib.parse.urlparse(url)
        if parsed.username or parsed.password:
            raise ValueError("Browser URLs with embedded credentials are not allowed.")
        opened = webbrowser.open_new_tab(url)
        output = f"Opened browser tab for {url}" if opened else f"Browser did not report success for {url}"
        return ToolResult(bool(opened), "open_browser", output, {"url": url, "opened": bool(opened)})

    def _open_vscode(self, path: str) -> ToolResult:
        target = self._safe_path(path or ".")
        code_command = self._vscode_command()
        if not code_command:
            return ToolResult(False, "open_vscode", "VS Code command `code` was not found on PATH or in the standard Windows install locations.", {"path": str(target)})
        command = [code_command, str(target)]
        flags = 0
        if os.name == "nt":
            flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        try:
            subprocess.Popen(command, cwd=str(self.workspace), creationflags=flags)
        except FileNotFoundError:
            return ToolResult(False, "open_vscode", f"VS Code command was not found: {code_command}", {"path": str(target), "command": code_command})
        return ToolResult(True, "open_vscode", f"Opened VS Code for {target}", {
            "path": str(target),
            "relative_path": "." if target == self.workspace else str(target.relative_to(self.workspace)),
            "command": code_command,
        })

    def _vscode_command(self) -> str:
        found = shutil.which("code") or shutil.which("code.cmd")
        if found:
            return found
        if os.name != "nt":
            return ""
        candidates = [
            Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "Microsoft VS Code" / "bin" / "code.cmd",
            Path(os.environ.get("PROGRAMFILES", "")) / "Microsoft VS Code" / "bin" / "code.cmd",
            Path(os.environ.get("PROGRAMFILES(X86)", "")) / "Microsoft VS Code" / "bin" / "code.cmd",
            Path.home() / "AppData" / "Local" / "Programs" / "Microsoft VS Code" / "bin" / "code.cmd",
        ]
        for candidate in candidates:
            if candidate.exists():
                return str(candidate)
        return ""
