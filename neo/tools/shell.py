from __future__ import annotations

import re
import shlex
import shutil
import subprocess

from ..config import Settings
from .base import ToolResult, ToolboxHelpers


class ShellTools(ToolboxHelpers):
    """Constrained PowerShell, WSL, and Python-snippet execution."""

    def _powershell(self, command: str) -> ToolResult:
        if not Settings.shell_enabled:
            return ToolResult(False, "powershell", "PowerShell tool is disabled.", {})
        blocked = self._blocked_command(command)
        if blocked:
            return ToolResult(False, "powershell", "Command blocked by harness policy.", {"command": command, "blocked": blocked})
        if self._looks_like_server_command(command):
            return self._start_process(command, self._process_name(command))
        result = subprocess.run(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", command],
            cwd=str(self.workspace),
            capture_output=True,
            text=True,
            timeout=Settings.tool_timeout_seconds,
        )
        output = (result.stdout or "") + (result.stderr or "")
        return ToolResult(result.returncode == 0, "powershell", output[-8000:], {"returncode": result.returncode})

    def _python(self, code: str) -> ToolResult:
        result = subprocess.run(
            ["python", "-c", code],
            cwd=str(self.workspace),
            capture_output=True,
            text=True,
            timeout=Settings.tool_timeout_seconds,
        )
        output = (result.stdout or "") + (result.stderr or "")
        return ToolResult(result.returncode == 0, "python", output[-8000:], {"returncode": result.returncode})

    def _wsl_status(self) -> ToolResult:
        wsl_path = shutil.which("wsl.exe") or shutil.which("wsl")
        if not wsl_path:
            return ToolResult(False, "wsl_status", "WSL is not available on PATH.", {"available": False, "distros": []})
        status = subprocess.run(
            [wsl_path, "--status"],
            capture_output=True,
            text=True,
            timeout=8,
        )
        listing = subprocess.run(
            [wsl_path, "-l", "-q"],
            capture_output=True,
            text=True,
            timeout=8,
        )
        status_text = self._process_text(status)
        list_text = self._process_text(listing)
        distros = [line.strip() for line in list_text.splitlines() if line.strip()]
        payload = {
            "available": True,
            "wsl_path": wsl_path,
            "distros": distros,
            "status_returncode": status.returncode,
            "list_returncode": listing.returncode,
        }
        output = "WSL available."
        if distros:
            output += "\nDistributions: " + ", ".join(distros)
        if status_text.strip():
            output += "\n\n" + status_text.strip()
        return ToolResult(True, "wsl_status", output[-8000:], payload)

    def _wsl_probe(self, distro: str = "") -> ToolResult:
        wsl_path = shutil.which("wsl.exe") or shutil.which("wsl")
        if not wsl_path:
            return ToolResult(False, "wsl_probe", "WSL is not available on PATH.", {"available": False})
        clean_distro = distro.strip()
        workspace_path = self._wsl_workspace_path(wsl_path, clean_distro)
        probe_command = (
            "printf 'pwd='; pwd; "
            "printf '\\nuname='; uname -a 2>/dev/null || true; "
            f"printf '\\nworkspace='; test -d {shlex.quote(workspace_path)} && printf present || printf missing"
        )
        args = [wsl_path]
        if clean_distro:
            args.extend(["-d", clean_distro])
        args.extend(["--", "bash", "-lc", f"cd {shlex.quote(workspace_path)} 2>/dev/null || true; {probe_command}"])
        result = subprocess.run(
            args,
            cwd=str(self.workspace),
            capture_output=True,
            text=True,
            timeout=8,
            env=self._safe_subprocess_env(),
        )
        output = self._bounded_output(self._process_text(result), 8000)
        payload = {
            "available": result.returncode == 0,
            "returncode": result.returncode,
            "distro": clean_distro,
            "workspace": workspace_path,
            "probe": output,
        }
        return ToolResult(result.returncode == 0, "wsl_probe", output or "WSL probe completed with no output.", payload)

    def _wsl(self, command: str, distro: str = "") -> ToolResult:
        if not Settings.shell_enabled:
            return ToolResult(False, "wsl", "WSL tool is disabled.", {})
        command = command.strip()
        if not command:
            return ToolResult(False, "wsl", "Command is required.", {})
        blocked = self._blocked_linux_command(command)
        if blocked:
            return ToolResult(False, "wsl", "Command blocked by harness policy.", {"command": command, "blocked": blocked})
        wsl_path = shutil.which("wsl.exe") or shutil.which("wsl")
        if not wsl_path:
            return ToolResult(False, "wsl", "WSL is not available on PATH.", {"available": False})

        clean_distro = distro.strip()
        workspace_path = self._wsl_workspace_path(wsl_path, clean_distro)
        wsl_command = f"cd {shlex.quote(workspace_path)} && {command}"
        args = [wsl_path]
        if clean_distro:
            args.extend(["-d", clean_distro])
        args.extend(["--", "bash", "-lc", wsl_command])
        result = subprocess.run(
            args,
            cwd=str(self.workspace),
            capture_output=True,
            text=True,
            timeout=Settings.tool_timeout_seconds,
        )
        output = self._process_text(result)
        return ToolResult(result.returncode == 0, "wsl", output[-12000:], {
            "returncode": result.returncode,
            "distro": clean_distro,
            "workspace": workspace_path,
        })

    def _wsl_workspace_path(self, wsl_path: str, distro: str = "") -> str:
        args = [wsl_path]
        if distro:
            args.extend(["-d", distro])
        args.extend(["--", "wslpath", "-a", str(self.workspace)])
        try:
            result = subprocess.run(args, capture_output=True, text=True, timeout=8)
            output = self._process_text(result).strip()
            if result.returncode == 0 and output:
                return output.splitlines()[-1].strip()
        except Exception:
            pass
        drive = self.workspace.drive.rstrip(":").lower() or "c"
        rest = self.workspace.as_posix().split(":", 1)[-1].lstrip("/")
        return f"/mnt/{drive}/{rest}"

    def _blocked_command(self, command: str) -> str | None:
        denied = ["remove-item", " del ", " rmdir", "format-volume", "shutdown", "stop-process", "set-executionpolicy"]
        lowered = f" {command.lower()} "
        return next((marker for marker in denied if marker in lowered), None)

    def _blocked_linux_command(self, command: str) -> str | None:
        lowered = f" {command.lower()} "
        denied = [
            " rm -rf /",
            " rm -fr /",
            " mkfs",
            " shutdown",
            " reboot",
            " halt",
            " poweroff",
            " dd if=",
            " :(){",
            " sudo rm",
        ]
        return next((marker for marker in denied if marker in lowered), None)

    def _looks_like_server_command(self, command: str) -> bool:
        lowered = command.lower()
        markers = [
            "flask run",
            "npm run dev",
            "npm start",
            "yarn dev",
            "pnpm dev",
            "vite",
            "uvicorn",
            "gunicorn",
            "python -m http.server",
            "manage.py runserver",
        ]
        if any(marker in lowered for marker in markers):
            return True
        return bool(re.search(r"\bpython(?:\.\w+)?(?:\s+\S+)*\s+\S*app\.py\b", lowered) or re.search(r"\bnode(?:\s+\S+)*\s+\S*server\.js\b", lowered))

    def _process_name(self, command: str) -> str:
        lowered = command.lower()
        if "flask" in lowered or "app.py" in lowered:
            return "flask_app"
        if "npm" in lowered or "vite" in lowered:
            return "frontend"
        if "node" in lowered:
            return "node_app"
        return "server"
