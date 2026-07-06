from __future__ import annotations

import json
import os
import re
import socket
import subprocess
import time
from pathlib import Path
from typing import Any, Dict, List

from ..config import ARTIFACTS_DIR


class ProcessManager:
    """Deterministic owner of long-running child processes.

    A start is only reported as ok when the child is actually serving:
    either an expected port begins listening, or the child survives a
    minimum alive window. A child that exits during the readiness window
    is a failure and carries its stderr/stdout tails as evidence.
    """

    def __init__(self, log_dir: Path | None = None) -> None:
        self.log_dir = (log_dir or ARTIFACTS_DIR / "processes").resolve()
        self._registry: Dict[int, Dict[str, Any]] = {}
        # The in-memory registry dies with each backend restart, leaving
        # orphaned children that block their ports forever (run 102: a stale
        # node pid on 3000). Persist entries so ANY backend instance can
        # recognize and reclaim listeners Neo itself started.
        self._registry_path = self.log_dir / "registry.json"
        self._persisted: Dict[int, Dict[str, Any]] = self._load_registry()

    def _load_registry(self) -> Dict[int, Dict[str, Any]]:
        try:
            raw = json.loads(self._registry_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        entries: Dict[int, Dict[str, Any]] = {}
        for pid, entry in (raw or {}).items():
            try:
                entries[int(pid)] = dict(entry)
            except (TypeError, ValueError):
                continue
        return entries

    def _save_registry(self) -> None:
        entries: Dict[str, Dict[str, Any]] = {}
        for pid, entry in self._persisted.items():
            entries[str(pid)] = entry
        for pid, entry in self._registry.items():
            entries[str(pid)] = {key: value for key, value in entry.items() if key != "process"}
        try:
            self.log_dir.mkdir(parents=True, exist_ok=True)
            self._registry_path.write_text(json.dumps(entries, indent=2), encoding="utf-8")
        except OSError:
            pass

    def known_pids(self) -> set[int]:
        return set(self._registry) | set(self._persisted)

    def owns_pid(self, pid: int) -> bool:
        """True when the pid (or one of its ancestors) is a process Neo
        started. Listeners are usually children of the registered shell
        wrapper, so the parent chain is walked a few hops."""
        known = self.known_pids()
        if not pid or not known:
            return False
        current = int(pid)
        for _ in range(4):
            if current in known:
                return True
            parent = self._parent_pid(current)
            if not parent or parent == current:
                return False
            current = parent
        return current in known

    def owner_agent_of(self, pid: int) -> Dict[str, Any] | None:
        """Ownership of a pid, resolved through the registered ancestor.

        Listeners are children of the registered shell wrapper, so a bare
        `registry[pid]` lookup misses them; walk the parent chain (like
        owns_pid) and return the owning entry that actually carries the
        owner_agent_id, plus whether that ancestor is a LIVE registration."""
        if not pid:
            return None
        known = self.known_pids()
        current = int(pid)
        for _ in range(4):
            if current in known:
                entry = self._registry.get(current) or self._persisted.get(current) or {}
                return {
                    "registered_pid": current,
                    "owner_agent_id": entry.get("owner_agent_id"),
                    "owner_agent_name": entry.get("owner_agent_name") or "",
                    "name": entry.get("name"),
                    "live": current in self._registry,
                }
            parent = self._parent_pid(current)
            if not parent or parent == current:
                return None
            current = parent
        return None

    def _parent_pid(self, pid: int) -> int | None:
        try:
            if os.name == "nt":
                result = subprocess.run(
                    ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command",
                     f"(Get-CimInstance Win32_Process -Filter \"ProcessId={int(pid)}\").ParentProcessId"],
                    capture_output=True,
                    text=True,
                    timeout=10,
                )
            else:
                result = subprocess.run(
                    ["ps", "-o", "ppid=", "-p", str(int(pid))],
                    capture_output=True,
                    text=True,
                    timeout=10,
                )
            value = (result.stdout or "").strip().splitlines()
            return int(value[0].strip()) if value and value[0].strip().isdigit() else None
        except Exception:
            return None

    def reclaim_port(self, port: int, listener_pid: int | None = None) -> bool:
        """Free a port occupied by a NEO-OWNED process (current or orphaned
        from a previous backend). Processes Neo did not start are never
        touched; those ports are routed around, not seized. Reclaim is only
        claimed after something was actually stopped: probing alone can
        misread a backlog-full listener as gone."""
        from ..tools.base import current_run_context

        requester = current_run_context().get("agent_id")
        candidates: List[int] = []
        if listener_pid and self.owns_pid(int(listener_pid)):
            candidates.append(int(listener_pid))
        else:
            for pid, entry in {**self._persisted, **{p: e for p, e in self._registry.items()}}.items():
                ports = entry.get("ports") or []
                if port in ports or entry.get("listening_port") == port:
                    candidates.append(int(pid))
        # Never seize a LIVE process another agent owns: with several agents
        # on one machine, "my port is busy" must mean "pick another port",
        # not "kill my teammate's server". Ownership is resolved through the
        # registered ANCESTOR (the listener is usually a child of the shell
        # wrapper). Orphans (previous backend) and the requester's own
        # processes stay reclaimable.
        filtered: List[int] = []
        for pid in candidates:
            owner_info = self.owner_agent_of(pid) or {}
            owner = owner_info.get("owner_agent_id")
            live_foreign = (
                owner_info.get("live")
                and owner is not None
                and requester is not None
                and int(owner) != int(requester)
            )
            if not live_foreign:
                filtered.append(pid)
        candidates = filtered
        if not candidates:
            return False
        for pid in candidates:
            self.stop(pid)
        deadline = time.time() + 3
        while time.time() < deadline:
            if not self.port_listening(port):
                return True
            time.sleep(0.2)
        return not self.port_listening(port)

    def start(
        self,
        command: str,
        name: str,
        cwd: Path,
        expected_ports: List[int] | None = None,
        env: Dict[str, str] | None = None,
        wait_seconds: float = 12.0,
        min_alive_seconds: float = 2.5,
    ) -> Dict[str, Any]:
        safe_name = re.sub(r"[^A-Za-z0-9_.-]+", "_", name or "process").strip("._") or "process"
        self.log_dir.mkdir(parents=True, exist_ok=True)
        stamp = str(int(time.time() * 1000))
        stdout_path = self.log_dir / f"{safe_name}_{stamp}.out.log"
        stderr_path = self.log_dir / f"{safe_name}_{stamp}.err.log"

        flags = 0
        if os.name == "nt":
            flags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) | getattr(subprocess, "CREATE_NO_WINDOW", 0)
        run_env = dict(os.environ)
        if env:
            run_env.update(env)

        with stdout_path.open("w", encoding="utf-8") as stdout_handle, stderr_path.open("w", encoding="utf-8") as stderr_handle:
            if os.name == "nt":
                argv = ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", command]
            else:
                argv = ["/bin/sh", "-lc", command]
            process = subprocess.Popen(
                argv,
                cwd=str(cwd),
                stdout=stdout_handle,
                stderr=stderr_handle,
                text=True,
                env=run_env,
                creationflags=flags,
            )

        from ..tools.base import current_run_context

        run_context = current_run_context()
        info: Dict[str, Any] = {
            "pid": process.pid,
            "name": safe_name,
            "command": command,
            "cwd": str(cwd),
            "ports": sorted(set(expected_ports or [])),
            "stdout_log": str(stdout_path),
            "stderr_log": str(stderr_path),
            "started_at": time.time(),
            # Ownership: which agent's run started this process. Sibling
            # agents may inspect it but not stop it or seize its port.
            "owner_agent_id": run_context.get("agent_id"),
            "owner_agent_name": run_context.get("agent_name") or "",
        }
        self._registry[process.pid] = {**info, "process": process}
        self._prune_dead_persisted()
        self._save_registry()

        readiness = self._wait_ready(process, info["ports"], wait_seconds, min_alive_seconds)
        info.update(readiness)
        if readiness["ready"] and not info.get("listening_port"):
            # Readiness by survival means no port was declared up front. The
            # server usually prints its address ("listening on port 3000");
            # recover it from the logs so downstream URL inference works
            # (run 71: app started fine but no URL could be inferred).
            discovered = self._discover_ports_from_logs(stdout_path, stderr_path)
            if discovered:
                info["ports"] = sorted(set(info["ports"]) | set(discovered))
                for port in discovered:
                    if self.port_listening(port):
                        info["listening_port"] = port
                        break
        if not readiness["ready"]:
            info["stdout_tail"] = self.tail(stdout_path)
            info["stderr_tail"] = self.tail(stderr_path)
        registered = self._registry.get(process.pid)
        if registered is not None:
            registered.update({key: value for key, value in info.items() if key != "process"})
        self._save_registry()
        return info

    def _prune_dead_persisted(self) -> None:
        stale = [pid for pid in self._persisted if not self.is_running(pid)]
        for pid in stale:
            self._persisted.pop(pid, None)

    def _discover_ports_from_logs(self, stdout_path: Path, stderr_path: Path, attempts: int = 4) -> List[int]:
        """Extract listening ports a child announced on stdout/stderr. Retries
        briefly: the child is alive but may not have flushed its banner yet."""
        patterns = (
            r"(?i)https?://(?:localhost|127\.0\.0\.1|0\.0\.0\.0|\[[^\]]*\]|[A-Za-z0-9.-]+):(\d{2,5})",
            r"(?i)\bport\b\s*[:=]?\s*(\d{2,5})\b",
            r"(?i)\blistening\s+(?:on|at)\s+:?(\d{2,5})\b",
        )
        for attempt in range(attempts):
            if attempt:
                time.sleep(0.5)
            text = self.tail(stdout_path) + "\n" + self.tail(stderr_path)
            ports: list[int] = []
            for pattern in patterns:
                for raw in re.findall(pattern, text):
                    try:
                        port = int(raw)
                    except ValueError:
                        continue
                    if 1 <= port <= 65535 and port not in ports:
                        ports.append(port)
            if ports:
                return ports
        return []

    def _wait_ready(
        self,
        process: subprocess.Popen,
        ports: List[int],
        wait_seconds: float,
        min_alive_seconds: float,
    ) -> Dict[str, Any]:
        deadline = time.time() + max(wait_seconds, min_alive_seconds)
        alive_since = time.time()
        while time.time() < deadline:
            exit_code = process.poll()
            if exit_code is not None:
                return {
                    "ready": False,
                    "running": False,
                    "returncode": exit_code,
                    "reason": f"process exited with code {exit_code} during startup",
                }
            for port in ports:
                if self.port_listening(port):
                    return {"ready": True, "running": True, "listening_port": port, "reason": "port listening"}
            if not ports and (time.time() - alive_since) >= min_alive_seconds:
                return {"ready": True, "running": True, "reason": f"process alive after {min_alive_seconds:.1f}s (no expected port declared)"}
            time.sleep(0.25)

        exit_code = process.poll()
        if exit_code is not None:
            return {
                "ready": False,
                "running": False,
                "returncode": exit_code,
                "reason": f"process exited with code {exit_code} during startup",
            }
        if ports:
            return {
                "ready": False,
                "running": True,
                "reason": f"process is alive but no expected port {ports} started listening within {wait_seconds:.0f}s",
            }
        return {"ready": True, "running": True, "reason": "process alive at readiness deadline"}

    def port_listening(self, port: int) -> bool:
        if port <= 0 or port > 65535:
            return False
        for host in ("127.0.0.1", "::1"):
            try:
                with socket.create_connection((host, port), timeout=0.2):
                    return True
            except OSError:
                continue
        return False

    def is_running(self, pid: int) -> bool:
        entry = self._registry.get(pid)
        if entry and entry.get("process") is not None:
            return entry["process"].poll() is None
        if os.name == "nt":
            try:
                result = subprocess.run(
                    ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command",
                     f"Get-Process -Id {pid} -ErrorAction SilentlyContinue | Select-Object -First 1 -ExpandProperty Id"],
                    capture_output=True,
                    text=True,
                    timeout=5,
                )
                return result.returncode == 0 and str(pid) in (result.stdout or "")
            except Exception:
                return False
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False

    def stop(self, pid: int) -> Dict[str, Any]:
        if pid <= 0:
            return {"stopped": False, "reason": "a valid pid is required"}
        was_running = self.is_running(pid)
        if not was_running:
            self._registry.pop(pid, None)
            return {"stopped": False, "running": False, "reason": f"process {pid} was not running"}
        try:
            if os.name == "nt":
                subprocess.run(
                    ["taskkill", "/PID", str(pid), "/T", "/F"],
                    capture_output=True,
                    text=True,
                    timeout=10,
                )
            else:
                os.kill(pid, 15)
        except Exception as exc:
            return {"stopped": False, "running": self.is_running(pid), "reason": str(exc)}
        time.sleep(0.2)
        still_running = self.is_running(pid)
        if not still_running:
            self._registry.pop(pid, None)
            self._persisted.pop(pid, None)
            self._save_registry()
        return {"stopped": not still_running, "running": still_running, "reason": "terminated" if not still_running else "termination requested but process is still alive"}

    def registry_entry(self, pid: int) -> Dict[str, Any] | None:
        entry = self._registry.get(pid)
        if not entry:
            return None
        return {key: value for key, value in entry.items() if key != "process"}

    def list_managed(self) -> List[Dict[str, Any]]:
        entries = []
        combined: Dict[int, Dict[str, Any]] = {}
        # Persisted entries from previous backend instances first, so live
        # registrations for the same pid win.
        for pid, entry in self._persisted.items():
            if pid not in self._registry:
                combined[pid] = {**entry, "orphaned": True}
        for pid, entry in self._registry.items():
            combined[pid] = entry
        for pid, entry in sorted(combined.items()):
            entries.append({
                "pid": pid,
                "name": entry.get("name"),
                "command": entry.get("command"),
                "cwd": entry.get("cwd"),
                "ports": entry.get("ports"),
                "listening_port": entry.get("listening_port"),
                "running": self.is_running(pid),
                "orphaned": bool(entry.get("orphaned")),
                "stdout_log": entry.get("stdout_log"),
                "stderr_log": entry.get("stderr_log"),
            })
        return entries

    def tail(self, path: Path | str, limit: int = 4000) -> str:
        target = Path(path)
        if not target.exists():
            return ""
        try:
            return target.read_text(encoding="utf-8", errors="replace")[-limit:]
        except OSError:
            return ""
