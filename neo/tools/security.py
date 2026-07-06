from __future__ import annotations

import ctypes
import json
import os
import platform
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

from ..config import Settings
from .base import ToolResult, ToolboxHelpers


class SecurityTools(ToolboxHelpers):
    """Read-only host, secrets, and dependency security audits.

    Everything here is bounded and redacted: no settings change, no secret
    values leave the tool, findings carry evidence previews only.
    """

    def _system_security_audit(self, scope: str) -> ToolResult:
        payload: dict[str, Any] = {
            "scope": scope.strip() or "host",
            "host": {
                "platform": platform.system(),
                "release": platform.release(),
                "version": platform.version(),
                "machine": platform.machine(),
                "python": platform.python_version(),
                "is_admin": self._is_admin(),
            },
            "checks": {
                "shell_enabled": Settings.shell_enabled,
                "tool_timeout_seconds": Settings.tool_timeout_seconds,
            },
            "findings": [],
            "limits": [
                "Read-only audit; no settings were changed.",
                "Secrets and environment values are not returned.",
                "Some OS checks require platform support and may report unavailable.",
            ],
        }
        if os.name == "nt":
            payload["checks"]["windows_defender"] = self._powershell_json(
                "try { Get-MpComputerStatus | Select-Object AntivirusEnabled,RealTimeProtectionEnabled,BehaviorMonitorEnabled,IoavProtectionEnabled,AntivirusSignatureAge,AntispywareSignatureAge,QuickScanAge,FullScanAge } catch { [pscustomobject]@{ error = $_.Exception.Message } }"
            )
            payload["checks"]["windows_firewall"] = self._powershell_json(
                "try { Get-NetFirewallProfile | Select-Object Name,Enabled,DefaultInboundAction,DefaultOutboundAction } catch { [pscustomobject]@{ error = $_.Exception.Message } }"
            )
            payload["checks"]["execution_policy"] = self._powershell_json(
                "try { Get-ExecutionPolicy -List | Select-Object Scope,ExecutionPolicy } catch { [pscustomobject]@{ error = $_.Exception.Message } }"
            )
            payload["checks"]["listening_ports"] = self._powershell_json(
                "try { Get-NetTCPConnection -State Listen | Sort-Object LocalPort | Select-Object -First 80 LocalAddress,LocalPort,OwningProcess } catch { [pscustomobject]@{ error = $_.Exception.Message } }"
            )
        else:
            payload["checks"]["listening_ports"] = self._posix_listening_ports()
            payload["checks"]["kernel"] = platform.platform()
        payload["checks"]["wsl"] = self._wsl_summary()
        payload["checks"]["workspace_secrets_hint"] = "Run secrets_scan for hardcoded credential exposure in the active workspace."
        payload["findings"] = self._security_findings(payload)
        return ToolResult(True, "system_security_audit", json.dumps(payload, indent=2, ensure_ascii=False), {
            "finding_count": len(payload["findings"]),
            "platform": payload["host"]["platform"],
        })

    def _secrets_scan(self, path: str, max_files: int) -> ToolResult:
        root = self._safe_path(path or ".")
        if not root.exists():
            return ToolResult(False, "secrets_scan", f"Path not found: {path}", {})
        max_files = max(1, min(max_files, 1200))
        findings: list[dict[str, Any]] = []
        scanned = 0
        for target in self._iter_security_scan_files(root, max_files):
            scanned += 1
            try:
                text = target.read_text(encoding="utf-8", errors="replace")
            except Exception:
                continue
            for line_number, line in enumerate(text.splitlines(), 1):
                for rule_name, pattern in self._secret_patterns():
                    if not pattern.search(line):
                        continue
                    findings.append({
                        "type": rule_name,
                        "path": str(target.relative_to(self.workspace)),
                        "line": line_number,
                        "preview": self._redact_sensitive(line.strip())[:220],
                    })
                    break
                if len(findings) >= 120:
                    break
            if len(findings) >= 120:
                break
        payload = {
            "path": "." if root == self.workspace else str(root.relative_to(self.workspace)),
            "files_scanned": scanned,
            "finding_count": len(findings),
            "findings": findings,
            "truncated": len(findings) >= 120,
        }
        return ToolResult(True, "secrets_scan", json.dumps(payload, indent=2, ensure_ascii=False), payload)

    def _dependency_audit(self, path: str, ecosystem: str) -> ToolResult:
        root = self._safe_path(path or ".")
        if root.is_file():
            root = root.parent
        if not root.exists() or not root.is_dir():
            return ToolResult(False, "dependency_audit", f"Project path is not a directory: {path}", {})
        ecosystem = (ecosystem or "auto").strip().lower()
        if ecosystem not in {"auto", "python", "pip", "node", "npm"}:
            return ToolResult(False, "dependency_audit", "ecosystem must be one of: auto, python, pip, node, npm", {"ecosystem": ecosystem})

        audits: list[dict[str, Any]] = []
        if ecosystem in {"auto", "python", "pip"}:
            audits.append(self._python_dependency_audit(root))
        if ecosystem in {"auto", "node", "npm"}:
            audits.append(self._node_dependency_audit(root))
        payload = {
            "path": "." if root == self.workspace else str(root.relative_to(self.workspace)),
            "audits": audits,
            "finding_count": sum(int(item.get("finding_count") or 0) for item in audits),
        }
        return ToolResult(True, "dependency_audit", json.dumps(payload, indent=2, ensure_ascii=False), payload)

    def _is_admin(self) -> bool:
        try:
            if os.name == "nt":
                return bool(ctypes.windll.shell32.IsUserAnAdmin())
            return hasattr(os, "geteuid") and os.geteuid() == 0
        except Exception:
            return False

    def _powershell_json(self, script: str) -> dict[str, Any]:
        executable = shutil.which("powershell") or shutil.which("pwsh")
        if not executable:
            return {"available": False, "error": "PowerShell was not found on PATH."}
        wrapped = f"& {{ {script} }} | ConvertTo-Json -Depth 6 -Compress"
        try:
            result = subprocess.run(
                [executable, "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", wrapped],
                cwd=str(self.workspace),
                capture_output=True,
                text=True,
                timeout=min(max(Settings.tool_timeout_seconds, 8), 20),
                env=self._safe_subprocess_env(),
            )
        except Exception as exc:
            return {"available": False, "error": str(exc)}
        output = self._bounded_output(self._process_text(result), 20000).strip()
        if result.returncode != 0:
            return {"available": False, "returncode": result.returncode, "error": output[-1000:]}
        if not output:
            return {"available": True, "data": None}
        try:
            return {"available": True, "data": json.loads(output)}
        except Exception:
            return {"available": True, "raw": output[-2000:]}

    def _posix_listening_ports(self) -> dict[str, Any]:
        for command in (["ss", "-ltn"], ["netstat", "-ltn"]):
            executable = shutil.which(command[0])
            if not executable:
                continue
            try:
                result = subprocess.run(
                    [executable, *command[1:]],
                    cwd=str(self.workspace),
                    capture_output=True,
                    text=True,
                    timeout=min(max(Settings.tool_timeout_seconds, 8), 20),
                    env=self._safe_subprocess_env(),
                )
            except Exception as exc:
                return {"available": False, "error": str(exc)}
            return {
                "available": result.returncode == 0,
                "command": command,
                "output": self._bounded_output(self._process_text(result), 12000),
            }
        return {"available": False, "error": "Neither ss nor netstat was found."}

    def _wsl_summary(self) -> dict[str, Any]:
        if not shutil.which("wsl"):
            return {"available": False}
        try:
            result = subprocess.run(
                ["wsl", "--status"],
                cwd=str(self.workspace),
                capture_output=True,
                text=True,
                timeout=10,
                env=self._safe_subprocess_env(),
            )
            distros = subprocess.run(
                ["wsl", "--list", "--verbose"],
                cwd=str(self.workspace),
                capture_output=True,
                text=True,
                timeout=10,
                env=self._safe_subprocess_env(),
            )
        except Exception as exc:
            return {"available": False, "error": str(exc)}
        return {
            "available": result.returncode == 0 or distros.returncode == 0,
            "status": self._bounded_output(self._process_text(result), 4000),
            "distros": self._bounded_output(self._process_text(distros), 4000),
        }

    def _security_findings(self, payload: dict[str, Any]) -> list[dict[str, str]]:
        findings: list[dict[str, str]] = []
        if payload.get("host", {}).get("is_admin"):
            findings.append({"severity": "medium", "area": "privilege", "message": "Neo is running with administrative privileges; prefer least-privilege for routine harness work."})

        defender = payload.get("checks", {}).get("windows_defender", {}).get("data")
        if isinstance(defender, dict):
            if defender.get("RealTimeProtectionEnabled") is False:
                findings.append({"severity": "high", "area": "antivirus", "message": "Windows Defender real-time protection appears disabled."})
            if defender.get("AntivirusEnabled") is False:
                findings.append({"severity": "high", "area": "antivirus", "message": "Windows Defender antivirus appears disabled."})

        firewall = payload.get("checks", {}).get("windows_firewall", {}).get("data")
        firewall_rows = firewall if isinstance(firewall, list) else ([firewall] if isinstance(firewall, dict) else [])
        for row in firewall_rows:
            if row.get("Enabled") is False:
                findings.append({"severity": "medium", "area": "firewall", "message": f"Firewall profile {row.get('Name', 'unknown')} appears disabled."})

        ports = payload.get("checks", {}).get("listening_ports", {}).get("data")
        port_rows = ports if isinstance(ports, list) else ([ports] if isinstance(ports, dict) else [])
        public_ports = []
        for row in port_rows:
            address = str(row.get("LocalAddress") or "")
            if address in {"0.0.0.0", "::", "[::]"}:
                public_ports.append(str(row.get("LocalPort") or ""))
        if public_ports:
            findings.append({"severity": "medium", "area": "network", "message": f"Listening ports bound on all interfaces: {', '.join(public_ports[:12])}."})
        if Settings.shell_enabled:
            findings.append({"severity": "info", "area": "harness", "message": "Shell tools are enabled; keep destructive command protections active."})
        return findings

    def _secret_patterns(self) -> list[tuple[str, re.Pattern[str]]]:
        return [
            ("private_key", re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----")),
            ("openai_key", re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b")),
            ("google_key", re.compile(r"\bAIza[0-9A-Za-z\-_]{20,}\b")),
            ("aws_access_key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
            ("credential_assignment", re.compile(r"(?i)\b(?:api[_-]?key|secret|token|password|credential|auth[_-]?token)\b\s*[:=]\s*['\"]?[^'\"\s]{8,}")),
        ]

    def _iter_security_scan_files(self, root: Path, max_files: int) -> list[Path]:
        skip_dirs = {".git", "node_modules", "venv", ".venv", "__pycache__", "dist", "build", ".neo"}
        if root.is_file():
            return [root] if self._is_scan_candidate(root) else []
        found: list[Path] = []
        for current, dirs, files in os.walk(root):
            dirs[:] = [name for name in dirs if name not in skip_dirs and not name.startswith(".pytest")]
            for file_name in files:
                target = Path(current) / file_name
                if not self._is_scan_candidate(target):
                    continue
                found.append(target)
                if len(found) >= max_files:
                    return found
        return found

    def _is_scan_candidate(self, target: Path) -> bool:
        if not target.is_file():
            return False
        try:
            if target.stat().st_size > 2_000_000:
                return False
            sample = target.read_bytes()[:4096]
        except Exception:
            return False
        if b"\x00" in sample:
            return False
        return True

    def _python_dependency_audit(self, root: Path) -> dict[str, Any]:
        requirements = root / "requirements.txt"
        pyproject = root / "pyproject.toml"
        if not requirements.exists() and not pyproject.exists():
            return {"ecosystem": "python", "status": "not_applicable", "reason": "No requirements.txt or pyproject.toml found.", "finding_count": 0}
        command = self._python_audit_command(requirements if requirements.exists() else None)
        if not command:
            return {"ecosystem": "python", "status": "not_run", "reason": "pip-audit is not installed.", "finding_count": 0}
        try:
            result = subprocess.run(
                command,
                cwd=str(root),
                capture_output=True,
                text=True,
                timeout=max(Settings.tool_timeout_seconds, 120),
                env=self._safe_subprocess_env(),
            )
        except Exception as exc:
            return {"ecosystem": "python", "status": "error", "error": str(exc), "finding_count": 0}
        output = self._bounded_output(self._process_text(result), 20000)
        finding_count = self._count_python_audit_findings(output)
        return {
            "ecosystem": "python",
            "status": "ran",
            "returncode": result.returncode,
            "command": command,
            "finding_count": finding_count,
            "output": output,
        }

    def _python_audit_command(self, requirements: Path | None) -> list[str]:
        executable = shutil.which("pip-audit")
        base = [executable] if executable else []
        if not base:
            probe = subprocess.run(
                [sys.executable, "-m", "pip_audit", "--version"],
                cwd=str(self.workspace),
                capture_output=True,
                text=True,
                timeout=10,
                env=self._safe_subprocess_env(),
            )
            if probe.returncode != 0:
                return []
            base = [sys.executable, "-m", "pip_audit"]
        command = [*base, "-f", "json"]
        if requirements:
            command.extend(["-r", str(requirements)])
        return command

    def _count_python_audit_findings(self, output: str) -> int:
        try:
            payload = json.loads(output)
        except Exception:
            return len(re.findall(r"(?i)\bvulnerability\b", output or ""))
        if isinstance(payload, dict):
            deps = payload.get("dependencies") or []
            if isinstance(deps, list):
                return sum(len(dep.get("vulns") or []) for dep in deps if isinstance(dep, dict))
        return 0

    def _node_dependency_audit(self, root: Path) -> dict[str, Any]:
        package_json = root / "package.json"
        if not package_json.exists():
            return {"ecosystem": "node", "status": "not_applicable", "reason": "No package.json found.", "finding_count": 0}
        if not (root / "package-lock.json").exists():
            return {"ecosystem": "node", "status": "not_run", "reason": "No package-lock.json found for npm audit.", "finding_count": 0}
        npm = shutil.which("npm")
        if not npm:
            return {"ecosystem": "node", "status": "not_run", "reason": "npm is not installed.", "finding_count": 0}
        command = [npm, "audit", "--json"]
        try:
            result = subprocess.run(
                command,
                cwd=str(root),
                capture_output=True,
                text=True,
                timeout=max(Settings.tool_timeout_seconds, 120),
                env=self._safe_subprocess_env(),
            )
        except Exception as exc:
            return {"ecosystem": "node", "status": "error", "error": str(exc), "finding_count": 0}
        output = self._bounded_output(self._process_text(result), 20000)
        finding_count = self._count_npm_audit_findings(output)
        return {
            "ecosystem": "node",
            "status": "ran",
            "returncode": result.returncode,
            "command": command,
            "finding_count": finding_count,
            "output": output,
        }

    def _count_npm_audit_findings(self, output: str) -> int:
        try:
            payload = json.loads(output)
        except Exception:
            return len(re.findall(r"(?i)\bvulnerab", output or ""))
        if isinstance(payload, dict):
            metadata = payload.get("metadata") or {}
            vulns = metadata.get("vulnerabilities") if isinstance(metadata, dict) else {}
            if isinstance(vulns, dict):
                return int(vulns.get("total") or sum(int(v) for k, v in vulns.items() if k != "total" and isinstance(v, int)))
            vulnerabilities = payload.get("vulnerabilities")
            if isinstance(vulnerabilities, dict):
                return len(vulnerabilities)
        return 0
