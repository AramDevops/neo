from __future__ import annotations

import importlib.metadata as importlib_metadata
import json
import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path

from ..config import Settings
from .base import ToolResult, ToolboxHelpers


class SetupTools(ToolboxHelpers):
    """Environment preparation: venvs, dependency installs, version probes."""

    def _python_venv(self, venv_path: str, requirements: str, install: bool) -> ToolResult:
        target = self._safe_path(venv_path or ".neo/venv")
        target.parent.mkdir(parents=True, exist_ok=True)
        result = subprocess.run(
            [sys.executable, "-m", "venv", str(target)],
            cwd=str(self.workspace),
            capture_output=True,
            text=True,
            timeout=max(Settings.tool_timeout_seconds, 60),
        )
        output = (result.stdout or "") + (result.stderr or "")
        if result.returncode != 0:
            return ToolResult(False, "python_venv", output[-8000:] or "Could not create Python virtual environment.", {"returncode": result.returncode, "venv_path": str(target)})

        python_path = target / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
        pip_output = ""
        installed = False
        req_path = self._safe_path(requirements or "requirements.txt")
        if install and req_path.exists():
            pip = subprocess.run(
                [str(python_path), "-m", "pip", "install", "-r", str(req_path)],
                cwd=str(self.workspace),
                capture_output=True,
                text=True,
                timeout=max(Settings.tool_timeout_seconds, 180),
            )
            pip_output = (pip.stdout or "") + (pip.stderr or "")
            if pip.returncode != 0:
                return ToolResult(False, "python_venv", pip_output[-8000:], {
                    "returncode": pip.returncode,
                    "venv_path": str(target),
                    "python": str(python_path),
                    "requirements": str(req_path),
                })
            installed = True

        payload = {
            "venv_path": str(target),
            "python": str(python_path),
            "requirements": str(req_path) if req_path.exists() else "",
            "installed_requirements": installed,
        }
        text = f"Python environment ready at {target}. Use {python_path} to run this project in isolation."
        if pip_output:
            text += "\n\n" + pip_output[-4000:]
        return ToolResult(True, "python_venv", text, payload)

    def _python_install(self, path: str, venv_path: str, requirements: str, create_venv: bool) -> ToolResult:
        root = self._safe_path(path or ".")
        if not root.exists() or not root.is_dir():
            return ToolResult(False, "python_install", f"Project path is not a directory: {path}", {"path": str(root)})
        req_path = self._safe_child_path(root, self._project_child_arg(root, requirements or "requirements.txt"))
        if not req_path.exists() or not req_path.is_file():
            return ToolResult(False, "python_install", "Requirements file was not found under the harness workspace.", {
                "path": str(root),
                "requirements": str(req_path),
            })
        target = self._safe_child_path(root, self._project_child_arg(root, venv_path or ".neo/venv"))
        python_path = target / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
        if create_venv or not python_path.exists():
            target.parent.mkdir(parents=True, exist_ok=True)
            venv = subprocess.run(
                [sys.executable, "-m", "venv", str(target)],
                cwd=str(root),
                capture_output=True,
                text=True,
                timeout=max(Settings.tool_timeout_seconds, 60),
                env=self._safe_subprocess_env(),
            )
            if venv.returncode != 0:
                return ToolResult(False, "python_install", self._bounded_output(self._process_text(venv), 8000) or "Could not create Python virtual environment.", {
                    "returncode": venv.returncode,
                    "venv_path": str(target),
                })
        if not python_path.exists():
            return ToolResult(False, "python_install", "Virtual environment Python was not found after setup.", {"python": str(python_path)})

        install = subprocess.run(
            [str(python_path), "-m", "pip", "install", "-r", str(req_path)],
            cwd=str(root),
            capture_output=True,
            text=True,
            timeout=max(Settings.tool_timeout_seconds, 180),
            env=self._safe_subprocess_env(),
        )
        output = self._bounded_output(self._process_text(install), 12000)
        payload = {
            "returncode": install.returncode,
            "path": str(root),
            "relative_path": "." if root == self.workspace else str(root.relative_to(self.workspace)),
            "venv_path": str(target),
            "python": str(python_path),
            "requirements": str(req_path),
        }
        if not output.strip():
            output = f"pip install exited with code {install.returncode}."
        return ToolResult(install.returncode == 0, "python_install", output, payload)

    def _node_missing_dependencies(self, root: Path) -> tuple[list[str], bool]:
        """(missing_packages, has_declared_deps). A declared package counts as
        installed when its directory exists under node_modules. This is the
        check-before-act state read: installs must never run blind."""
        package_path = root / "package.json"
        try:
            package = json.loads(package_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return [], False
        declared: list[str] = []
        for key in ("dependencies", "devDependencies"):
            section = package.get(key) if isinstance(package, dict) else None
            if isinstance(section, dict):
                declared.extend(str(name) for name in section)
        if not declared:
            return [], False
        modules = root / "node_modules"
        missing = [name for name in declared if not (modules / name).exists()]
        return missing, True

    def _node_install(self, path: str, package_manager: str, frozen: bool, production: bool, force: bool = False) -> ToolResult:
        root = self._safe_path(path or ".")
        if not root.exists() or not root.is_dir():
            return ToolResult(False, "node_install", f"Project path is not a directory: {path}", {"path": str(root)})
        package_path = root / "package.json"
        if not package_path.exists() or not package_path.is_file():
            return ToolResult(False, "node_install", "package.json was not found under the requested workspace path.", {"path": str(root)})

        missing, has_declared = self._node_missing_dependencies(root)
        if not force and not missing:
            detail = (
                "all declared dependencies are already present in node_modules"
                if has_declared else "package.json declares no dependencies"
            )
            return ToolResult(True, "node_install", f"Dependencies already installed ({detail}); skipped the install. Pass force=true to reinstall.", {
                "path": str(root),
                "relative_path": "." if root == self.workspace else str(root.relative_to(self.workspace)),
                "skipped": True,
                "already_installed": True,
            })

        manager = self._detect_node_package_manager(root, package_manager)
        executable = shutil.which(manager)
        if not executable:
            return ToolResult(False, "node_install", f"{manager} was not found on PATH.", {"package_manager": manager, "path": str(root)})
        command = self._node_install_command(manager, root, frozen, production)
        # On Windows npm/pnpm/yarn are .cmd shims: CreateProcess cannot launch
        # the bare name ("npm" -> WinError 2), only the which-resolved path.
        command[0] = executable
        result = subprocess.run(
            command,
            cwd=str(root),
            capture_output=True,
            text=True,
            timeout=max(Settings.tool_timeout_seconds, 180),
            env=self._safe_subprocess_env(),
        )
        output = self._bounded_output(self._process_text(result), 12000)
        if result.returncode != 0 and "ci" in command and ("EUSAGE" in output or "in sync" in output):
            # `npm ci` demands a lockfile in sync with package.json, which is
            # NEVER true right after dependencies were added to package.json
            # (run 122 failed here; run 123 escaped by DELETING the lockfile).
            # A stale lock is a deterministic, harness-fixable state: fall back
            # to a regular install, which also refreshes the lockfile.
            fallback = [executable, "install", *(["--omit", "dev"] if production else [])]
            result = subprocess.run(
                fallback,
                cwd=str(root),
                capture_output=True,
                text=True,
                timeout=max(Settings.tool_timeout_seconds, 180),
                env=self._safe_subprocess_env(),
            )
            command = fallback
            fallback_output = self._bounded_output(self._process_text(result), 12000)
            output = (
                "package-lock.json was out of sync with package.json (expected right after adding "
                "dependencies), so `npm ci` was automatically replaced with `npm install` to refresh it.\n"
                + fallback_output
            )
        payload = {
            "returncode": result.returncode,
            "path": str(root),
            "relative_path": "." if root == self.workspace else str(root.relative_to(self.workspace)),
            "package_manager": manager,
            "command": command,
            "frozen": frozen,
            "production": production,
        }
        if not output.strip():
            output = f"{manager} install exited with code {result.returncode}."
        return ToolResult(result.returncode == 0, "node_install", output, payload)

    def _detect_node_package_manager(self, root: Path, package_manager: str) -> str:
        requested = (package_manager or "auto").strip().lower()
        allowed = {"auto", "npm", "pnpm", "yarn"}
        if requested not in allowed:
            raise ValueError("package_manager must be one of: auto, npm, pnpm, yarn")
        if requested != "auto":
            return requested
        if (root / "pnpm-lock.yaml").exists():
            return "pnpm"
        if (root / "yarn.lock").exists():
            return "yarn"
        return "npm"

    def _node_install_command(self, package_manager: str, root: Path, frozen: bool, production: bool) -> list[str]:
        if package_manager == "npm":
            command = ["npm", "ci" if frozen and (root / "package-lock.json").exists() else "install"]
            if production:
                command.extend(["--omit", "dev"])
            return command
        if package_manager == "pnpm":
            command = ["pnpm", "install"]
            if frozen:
                command.append("--frozen-lockfile")
            if production:
                command.append("--prod")
            return command
        command = ["yarn", "install"]
        if frozen:
            command.append("--frozen-lockfile")
        if production:
            command.append("--production=true")
        return command

    def _tool_versions(self) -> ToolResult:
        probes = {
            "python": [sys.executable, "--version"],
            "pip": [sys.executable, "-m", "pip", "--version"],
            "pytest": [sys.executable, "-m", "pytest", "--version"],
            "node": ["node", "--version"],
            "npm": ["npm", "--version"],
            "pnpm": ["pnpm", "--version"],
            "yarn": ["yarn", "--version"],
            "git": ["git", "--version"],
        }
        if os.name == "nt":
            probes["powershell"] = ["powershell", "-NoProfile", "-Command", "$PSVersionTable.PSVersion.ToString()"]
            probes["wsl"] = ["wsl", "--version"]

        tools = {name: self._version_probe(command) for name, command in probes.items()}
        packages = {}
        for package_name in ["flask", "pytest", "openai", "google-genai", "pymysql"]:
            try:
                packages[package_name] = importlib_metadata.version(package_name)
            except importlib_metadata.PackageNotFoundError:
                packages[package_name] = None
        payload = {
            "platform": {
                "system": platform.system(),
                "release": platform.release(),
                "machine": platform.machine(),
                "python": platform.python_version(),
            },
            "workspace": str(self.workspace),
            "tools": tools,
            "python_packages": packages,
        }
        return ToolResult(True, "tool_versions", json.dumps(payload, indent=2), payload)

    def _version_probe(self, command: list[str]) -> dict:
        executable = command[0]
        path = executable if Path(executable).exists() else shutil.which(executable)
        if not path:
            return {"available": False, "path": ""}
        try:
            result = subprocess.run(
                [str(path), *command[1:]],
                cwd=str(self.workspace),
                capture_output=True,
                text=True,
                timeout=8,
                env=self._safe_subprocess_env(),
            )
        except Exception as exc:
            return {"available": False, "path": str(path), "error": str(exc), "error_type": type(exc).__name__}
        output = self._bounded_output(self._process_text(result), 1200).strip()
        return {
            "available": result.returncode == 0,
            "path": str(path),
            "version": output.splitlines()[0] if output else "",
            "returncode": result.returncode,
        }

    def _project_child_arg(self, root: Path, path: str) -> str:
        raw = str(path or "").strip()
        if not raw:
            return raw
        raw_path = Path(raw)
        if raw_path.is_absolute():
            try:
                return str(raw_path.resolve().relative_to(root.resolve()))
            except ValueError:
                return raw
        clean = raw.replace("\\", "/").strip("/")
        try:
            root_rel = str(root.resolve().relative_to(self.workspace)).replace("\\", "/").strip("/")
        except ValueError:
            return raw
        if root_rel and clean.startswith(f"{root_rel}/"):
            return clean[len(root_rel) + 1:] or "."
        return raw
