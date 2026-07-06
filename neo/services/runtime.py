from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ..config import ARTIFACTS_DIR, WORKSPACE_DIR


RUNTIME_PATH = ARTIFACTS_DIR / "runtime_settings.json"


def _load() -> dict[str, Any]:
    if not RUNTIME_PATH.exists():
        return {}
    try:
        return json.loads(RUNTIME_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save(data: dict[str, Any]) -> None:
    RUNTIME_PATH.parent.mkdir(parents=True, exist_ok=True)
    RUNTIME_PATH.write_text(json.dumps(data, indent=2), encoding="utf-8")


def runtime_settings() -> dict[str, Any]:
    return _load()


def save_runtime_settings(data: dict[str, Any]) -> None:
    _save(data)


def get_workspace_dir() -> Path:
    data = _load()
    raw = data.get("workspace_dir")
    if raw:
        return Path(str(raw)).expanduser().resolve()
    return WORKSPACE_DIR.resolve()


def set_workspace_dir(path: str, create: bool = True) -> Path:
    if not path or not path.strip():
        raise ValueError("Workspace path is required.")
    target = Path(path).expanduser().resolve()
    if target.exists() and not target.is_dir():
        raise ValueError("Workspace path exists but is not a directory.")
    if not target.exists():
        if not create:
            raise ValueError("Workspace path does not exist.")
        target.mkdir(parents=True, exist_ok=True)
    data = _load()
    data["workspace_dir"] = str(target)
    _save(data)
    return target


def pick_workspace_dir(initial: str | None = None) -> str:
    try:
        import tkinter as tk
        from tkinter import filedialog
    except Exception as exc:
        raise RuntimeError("Native folder picker is unavailable in this Python environment.") from exc

    initial_path = Path(initial or get_workspace_dir()).expanduser()
    if not initial_path.exists() or not initial_path.is_dir():
        initial_path = WORKSPACE_DIR

    root = tk.Tk()
    root.withdraw()
    root.attributes("-topmost", True)
    try:
        selected = filedialog.askdirectory(
            parent=root,
            initialdir=str(initial_path.resolve()),
            title="Select Neo working directory",
            mustexist=False,
        )
    finally:
        root.destroy()
    return str(Path(selected).resolve()) if selected else ""


def workspace_status() -> dict[str, Any]:
    path = get_workspace_dir()
    exists = path.exists()
    writable = False
    if exists and path.is_dir():
        probe = path / ".neo_write_probe"
        try:
            probe.write_text("ok", encoding="utf-8")
            probe.unlink(missing_ok=True)
            writable = True
        except Exception:
            writable = False
    return {
        "workspace_dir": str(path),
        "exists": exists,
        "is_dir": path.is_dir() if exists else False,
        "writable": writable,
        "runtime_settings_path": str(RUNTIME_PATH),
    }
