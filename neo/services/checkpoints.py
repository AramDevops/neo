from __future__ import annotations

import shutil
import subprocess
import threading
from pathlib import Path
from typing import Any, Callable, Dict, List

from ..config import Settings


# Serializes every git operation across ALL CheckpointStore instances (the
# runner, the /rollback command, and the API endpoint each build their own,
# but they share one shadow repo). Without this, concurrent agent runs race on
# the repo's single index.lock and silently drop a checkpoint. Git ops are
# millisecond-scale, so one process-wide lock is contention-free in practice.
_GIT_LOCK = threading.RLock()


# Directories that must never be snapshotted: our own shadow repo, dependency
# trees, virtualenvs, caches. Kept out of every checkpoint so commits stay
# source-sized and rollback never touches an installed node_modules/venv.
_EXCLUDES = [
    ".neo/",
    "node_modules/",
    ".venv/",
    "venv/",
    "__pycache__/",
    "*.pyc",
    ".pytest_cache/",
    "dist/",
    "build/",
    "*.log",
]


class CheckpointStore:
    """Workspace snapshots backed by a SHADOW git repo.

    Every commit lives in <workspace>/.neo/checkpoints.git with an explicit
    --work-tree, so the user's own .git (if any) is never read or written.
    This gives each run a "before" snapshot the user can roll back to, the
    reversible-edit safety net every production coding harness ships.
    """

    def __init__(self, workspace_getter: Callable[[], Path]) -> None:
        self._workspace_getter = workspace_getter

    @property
    def workspace(self) -> Path:
        return Path(self._workspace_getter()).resolve()

    @property
    def git_dir(self) -> Path:
        return self.workspace / ".neo" / "checkpoints.git"

    def enabled(self) -> bool:
        return bool(Settings.checkpoints_enabled) and shutil.which("git") is not None

    def _git(self, args: List[str], timeout: int = 30) -> subprocess.CompletedProcess[str]:
        # Identity is passed per-invocation so checkpoints work without any
        # global git config, and signing is forced off so a user's global
        # commit.gpgsign never blocks a snapshot.
        base = [
            "git",
            "-c", "user.name=Neo",
            "-c", "user.email=neo@localhost",
            "-c", "commit.gpgsign=false",
            "-c", "core.autocrlf=false",
            "--git-dir", str(self.git_dir),
            "--work-tree", str(self.workspace),
        ]
        return subprocess.run(
            base + args,
            capture_output=True,
            text=True,
            timeout=timeout,
        )

    def ensure_repo(self) -> bool:
        if not self.enabled():
            return False
        try:
            if not (self.git_dir / "HEAD").exists():
                self.git_dir.parent.mkdir(parents=True, exist_ok=True)
                init = self._git(["init", "-q"])
                if init.returncode != 0:
                    return False
                # info/exclude keeps the shadow repo from ever staging heavy or
                # internal trees, on top of any .gitignore already present.
                exclude_path = self.git_dir / "info" / "exclude"
                exclude_path.parent.mkdir(parents=True, exist_ok=True)
                exclude_path.write_text("\n".join(_EXCLUDES) + "\n", encoding="utf-8")
            return True
        except Exception:
            return False

    def _clear_stale_index_lock(self) -> None:
        # Only ever called while holding _GIT_LOCK, so no OTHER Neo git op is
        # running; a lingering index.lock is therefore stale (a git child that
        # was killed by a timeout) and safe to remove. The shadow repo is
        # Neo-private, so there is no foreign git process to race.
        lock = self.git_dir / "index.lock"
        try:
            if lock.exists():
                lock.unlink()
        except OSError:
            pass

    def checkpoint(self, label: str) -> Dict[str, Any] | None:
        """Snapshot the workspace. Returns {id, label} or None (best-effort:
        a checkpoint failure must never break a run)."""
        with _GIT_LOCK:
            if not self.ensure_repo():
                return None
            try:
                self._clear_stale_index_lock()
                add = self._git(["add", "-A"], timeout=120)
                if add.returncode != 0:
                    # A stale lock from a prior killed git can block add; clear
                    # it and retry once before giving up.
                    self._clear_stale_index_lock()
                    add = self._git(["add", "-A"], timeout=120)
                    if add.returncode != 0:
                        return None
                # --allow-empty so a run that changes nothing still gets a stable
                # "before" marker to roll back to.
                commit = self._git(["commit", "--allow-empty", "-q", "-m", label[:200]], timeout=60)
                if commit.returncode != 0:
                    return None
                # Store the FULL sha: an abbreviated prefix becomes ambiguous
                # over a long-lived repo, which would make a run un-rollbackable.
                head = self._git(["rev-parse", "HEAD"])
                if head.returncode != 0:
                    return None
                return {"id": head.stdout.strip(), "label": label[:200]}
            except Exception:
                return None

    def list(self, limit: int = 20) -> List[Dict[str, Any]]:
        if not self.enabled() or not (self.git_dir / "HEAD").exists():
            return []
        try:
            limit = max(1, min(int(limit), 100))
            # \x1f (unit separator) cannot appear in a git short-sha/subject,
            # so it is a safe field delimiter for parsing.
            log = self._git(["log", f"-n{limit}", "--pretty=format:%h\x1f%ct\x1f%s"])
            if log.returncode != 0:
                return []
            entries: List[Dict[str, Any]] = []
            for line in log.stdout.splitlines():
                parts = line.split("\x1f")
                if len(parts) != 3:
                    continue
                entries.append({
                    "id": parts[0],
                    "created_at": int(parts[1]) if parts[1].isdigit() else 0,
                    "label": parts[2],
                })
            return entries
        except Exception:
            return []

    def changes_since(self, checkpoint_id: str) -> Dict[str, Any] | None:
        """name-status diff of the working tree against a checkpoint."""
        if not self.enabled() or not self._is_commit(checkpoint_id):
            return None
        try:
            files: List[Dict[str, str]] = []
            seen: set[str] = set()
            # Tracked changes vs the checkpoint (modified/deleted/renamed).
            diff = self._git(["diff", "--name-status", checkpoint_id])
            if diff.returncode != 0:
                return None
            for line in diff.stdout.splitlines():
                bits = line.split("\t")
                if len(bits) >= 2:
                    path = bits[-1].strip()
                    files.append({"status": bits[0].strip()[:1], "path": path})
                    seen.add(path)
            # Untracked new files (created since the checkpoint, not ignored):
            # git diff never lists these, but rollback WILL remove them.
            others = self._git(["ls-files", "--others", "--exclude-standard"])
            if others.returncode == 0:
                for path in others.stdout.splitlines():
                    clean = path.strip()
                    if clean and clean not in seen:
                        files.append({"status": "A", "path": clean})
                        seen.add(clean)
            return {"checkpoint_id": checkpoint_id, "files": files}
        except Exception:
            return None

    def _is_commit(self, checkpoint_id: str) -> bool:
        clean = (checkpoint_id or "").strip()
        if not clean or not self.enabled() or not (self.git_dir / "HEAD").exists():
            return False
        probe = self._git(["cat-file", "-e", f"{clean}^{{commit}}"])
        return probe.returncode == 0

    def rollback(self, checkpoint_id: str) -> Dict[str, Any]:
        """Restore the workspace to a checkpoint: reset tracked files and
        remove new (untracked, non-ignored) files created since. Ignored trees
        (node_modules, venv, .neo) are left intact by design."""
        clean = (checkpoint_id or "").strip()
        if not self.enabled():
            return {"ok": False, "reason": "checkpoints are unavailable (git not found or disabled)"}
        with _GIT_LOCK:
            if not self._is_commit(clean):
                return {"ok": False, "reason": f"unknown checkpoint: {checkpoint_id}"}
            try:
                changed = self.changes_since(clean) or {"files": []}
                self._clear_stale_index_lock()
                reset = self._git(["reset", "--hard", clean], timeout=60)
                if reset.returncode != 0:
                    return {"ok": False, "reason": (reset.stderr or reset.stdout or "reset failed").strip()[:400]}
                # Remove files created after the checkpoint (respects excludes and
                # .gitignore, so ignored trees survive).
                self._git(["clean", "-fd"], timeout=60)
                return {"ok": True, "checkpoint_id": clean, "reverted_files": changed.get("files", [])}
            except Exception as exc:
                return {"ok": False, "reason": str(exc)[:400]}

    def latest_id(self) -> str | None:
        entries = self.list(limit=1)
        return entries[0]["id"] if entries else None
