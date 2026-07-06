from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

from ..config import Settings
from .base import ToolResult, ToolboxHelpers


class QualityTools(ToolboxHelpers):
    """Syntax checks and allowlisted test runners."""

    def _syntax_check(self, path: str, max_files: int) -> ToolResult:
        import ast

        target = self._safe_path(path or ".")
        if not target.exists():
            return ToolResult(False, "syntax_check", f"Path not found: {path}", {})
        max_files = max(1, min(max_files, 200))
        files = self._syntax_targets(target, max_files)
        node_path = shutil.which("node")
        checked = []
        skipped = []
        issues = []
        for file_path in files:
            rel = str(file_path.relative_to(self.workspace))
            suffix = file_path.suffix.lower()
            try:
                if suffix == ".py":
                    ast.parse(file_path.read_text(encoding="utf-8", errors="replace"), filename=rel)
                    checked.append({"path": rel, "kind": "python"})
                elif suffix == ".json":
                    json.loads(file_path.read_text(encoding="utf-8", errors="replace"))
                    checked.append({"path": rel, "kind": "json"})
                elif suffix in {".js", ".mjs", ".cjs"}:
                    if not node_path:
                        skipped.append({"path": rel, "reason": "node not available"})
                        continue
                    result = subprocess.run(
                        [node_path, "--check", str(file_path)],
                        cwd=str(self.workspace),
                        capture_output=True,
                        text=True,
                        timeout=Settings.tool_timeout_seconds,
                        env=self._safe_subprocess_env(),
                    )
                    checked.append({"path": rel, "kind": "javascript", "returncode": result.returncode})
                    if result.returncode != 0:
                        issues.append({"path": rel, "message": self._bounded_output(self._process_text(result), 2000)})
                else:
                    skipped.append({"path": rel, "reason": "unsupported extension"})
            except SyntaxError as exc:
                issues.append({"path": rel, "line": exc.lineno, "offset": exc.offset, "message": exc.msg})
            except json.JSONDecodeError as exc:
                issues.append({"path": rel, "line": exc.lineno, "column": exc.colno, "message": exc.msg})
            except Exception as exc:
                issues.append({"path": rel, "message": str(exc), "error_type": type(exc).__name__})
        payload = {
            "ok": not issues,
            "checked": checked,
            "skipped": skipped[:50],
            "issues": issues,
            "truncated": target.is_dir() and len(files) >= max_files,
        }
        return ToolResult(not issues, "syntax_check", json.dumps(payload, indent=2), payload)

    def _syntax_targets(self, target: Path, max_files: int) -> list[Path]:
        supported = {".py", ".json", ".js", ".mjs", ".cjs"}
        ignored_parts = {".git", "node_modules", "venv", ".venv", "__pycache__", ".pytest_cache"}
        if target.is_file():
            return [target]
        files = []
        for file_path in sorted(target.rglob("*"), key=lambda p: str(p.relative_to(self.workspace)).lower()):
            if len(files) >= max_files:
                break
            if not file_path.is_file() or file_path.suffix.lower() not in supported:
                continue
            if any(part in ignored_parts for part in file_path.relative_to(target).parts):
                continue
            if file_path.stat().st_size > 1_000_000:
                continue
            files.append(file_path)
        return files

    def _run_tests(self, runner: str, path: str, maxfail: int, timeout_seconds: int) -> ToolResult:
        runner = (runner or "pytest").strip().lower()
        if runner not in {"pytest", "npm"}:
            return ToolResult(False, "run_tests", "runner must be one of: pytest, npm", {"runner": runner})
        target = self._safe_path(path or ".")
        if not target.exists():
            return ToolResult(False, "run_tests", f"Path not found: {path}", {})
        maxfail = max(1, min(maxfail, 20))
        timeout = max(5, min(timeout_seconds or max(Settings.tool_timeout_seconds, 60), 300))
        if runner == "pytest":
            rel = "." if target == self.workspace else str(target.relative_to(self.workspace))
            command = [sys.executable, "-m", "pytest", rel, "-q", "--tb=short", "--disable-warnings", f"--maxfail={maxfail}"]
            cwd = self.workspace
        else:
            root = target if target.is_dir() else target.parent
            package_path = root / "package.json"
            if not package_path.exists():
                return ToolResult(False, "run_tests", "package.json was not found for npm test.", {"path": str(root)})
            try:
                package = json.loads(package_path.read_text(encoding="utf-8"))
            except Exception as exc:
                return ToolResult(False, "run_tests", f"Could not read package.json: {exc}", {"path": str(package_path)})
            scripts = package.get("scripts") if isinstance(package, dict) else {}
            if not isinstance(scripts, dict) or "test" not in scripts:
                return ToolResult(False, "run_tests", "package.json does not define a test script.", {"path": str(package_path)})
            npm = shutil.which("npm")
            if not npm:
                return ToolResult(False, "run_tests", "npm was not found on PATH.", {"path": str(root)})
            command = [npm, "test"]
            cwd = root
        result = subprocess.run(
            command,
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=timeout,
            env=self._safe_subprocess_env(),
        )
        output = self._bounded_output(self._process_text(result), 12000)
        payload = {
            "runner": runner,
            "returncode": result.returncode,
            "command": command,
            "path": str(target),
            "relative_path": "." if target == self.workspace else str(target.relative_to(self.workspace)),
            "timeout_seconds": timeout,
        }
        if not output.strip():
            output = f"{runner} exited with code {result.returncode}."
        return ToolResult(result.returncode == 0, "run_tests", output, payload)
