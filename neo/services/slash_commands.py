from __future__ import annotations

"""Deterministic slash-command layer in front of the agent loop.

Messages starting with "/" are handled by the backend instantly, no model
call, no run. They control harness settings and expose tool/status
information, so they behave identically in every client.
"""

import json
from typing import Any

from . import computer_access
from .models import model_catalog
from .provider_runtime import default_engine, save_engine
from .runtime import workspace_status


HELP_TEXT = """Available commands:
/help - this list
/clear - wipe this terminal's chat history
/role [instructions|clear] - show or set this agent's durable role (specialist system prompt)
/scope [glob ...|clear] - show or set this agent's enforced write scope, e.g. /scope server/* shared/*
/ls [path] - list the full contents of a workspace directory
/tree [path] - show the workspace directory tree
/pwd - print the active workspace directory
/cat <file> - print a workspace file
/mkdir <dir ...> - create directories
/touch <file ...> - create empty files
/cp [-r] <src> <dst> - copy a file (or a directory with -r)
/mv <src> <dst> - move or rename a file or directory
/rm [-r] <path ...> - delete files (or non-empty directories with -r)
/grep <pattern> [path] - regex search file contents
/find <glob> [path] - find files by name pattern, e.g. /find *.py
/model - show the current engine and list available models
/model <model> - switch model (provider inferred), e.g. /model gemini-2.5-pro
/model <provider> <model> - switch provider and model, e.g. /model claude claude-sonnet-5
/tools [category] - tool catalog (grouped by category)
/access - computer-control permission status and policy
/access grant [minutes] - grant timed computer control (ask mode)
/access revoke - revoke the active grant
/access full | ask - switch the computer-control mode
/status - harness metrics snapshot
/workspace - active workspace status
/checkpoints - list recent workspace snapshots (taken before each run)
/rollback [id] - undo a run's file changes (no id = undo the most recent run)
File commands run instantly against the workspace sandbox, no model call.
Anything else is sent to the agent as a normal message."""


def is_command(text: str) -> bool:
    clean = (text or "").strip()
    return clean.startswith("/") and len(clean) > 1 and not clean.startswith("//")


def handle(toolbox: Any, text: str) -> str:
    parts = (text or "").strip().split()
    name = parts[0].lstrip("/").lower()
    args = parts[1:]
    if name in {"help", "?"}:
        return HELP_TEXT
    if name in {"clear", "reset"}:
        # Handled at the API layer (it owns the message history); reaching this
        # fallback means the client did not intercept it.
        return "/clear wipes this terminal's chat history. This client does not support it."
    if name in {"ls", "dir"}:
        return _ls(toolbox, args[0] if args else ".")
    if name == "tree":
        return _tree(toolbox, args[0] if args else ".")
    if name == "pwd":
        return workspace_status()["workspace_dir"]
    if name in {"cat", "type"}:
        return _cat(toolbox, args)
    if name in {"mkdir", "md"}:
        return _mkdir(toolbox, args)
    if name == "touch":
        return _touch(toolbox, args)
    if name in {"cp", "copy"}:
        return _cp(toolbox, args)
    if name in {"mv", "move", "rename"}:
        return _mv(toolbox, args)
    if name in {"rm", "del"}:
        return _rm(toolbox, args)
    if name == "grep":
        return _grep(toolbox, args)
    if name == "find":
        return _find(toolbox, args)
    if name in {"model", "engine"}:
        return _model(args)
    if name == "tools":
        return _tools(toolbox, args[0].lower() if args else "")
    if name == "access":
        return _access(args)
    if name == "status":
        result = toolbox.execute("metrics_snapshot", {})
        return result.output if result.ok else f"Status unavailable: {result.output}"
    if name == "workspace":
        status = workspace_status()
        return (
            f"Workspace: {status['workspace_dir']}\n"
            f"exists: {status['exists']} | writable: {status['writable']}"
        )
    if name in {"checkpoints", "history", "snapshots"}:
        return _checkpoints(toolbox)
    if name in {"rollback", "revert", "undo"}:
        return _rollback(toolbox, args[0] if args else "")
    return f"Unknown command /{name}.\n\n{HELP_TEXT}"


def _checkpoint_store(toolbox: Any):
    from .checkpoints import CheckpointStore

    return CheckpointStore(lambda: toolbox.workspace)


def _checkpoints(toolbox: Any) -> str:
    store = _checkpoint_store(toolbox)
    if not store.enabled():
        return "Workspace checkpoints are unavailable (git is not installed or checkpoints are disabled)."
    entries = store.list(20)
    if not entries:
        return "No checkpoints yet. Neo snapshots the workspace before each run that changes files."
    lines = ["Recent workspace checkpoints (newest first):"]
    for entry in entries:
        lines.append(f"  {entry['id']}  {entry['label']}")
    lines.append("")
    lines.append("Undo a run's changes with /rollback <id>, or /rollback with no id to undo the most recent run.")
    return "\n".join(lines)


def _rollback(toolbox: Any, checkpoint_id: str) -> str:
    store = _checkpoint_store(toolbox)
    if not store.enabled():
        return "Workspace checkpoints are unavailable (git is not installed or checkpoints are disabled)."
    from ..db import Database

    busy = Database().fetchone("SELECT COUNT(*) AS n FROM runs WHERE status=?", ("running",))
    if busy and int(busy.get("n") or 0) > 0:
        return "A terminal is currently running. Stop it or wait for it to finish before rolling back the workspace."
    target = checkpoint_id.strip() or (store.latest_id() or "")
    if not target:
        return "No checkpoint to roll back to yet."
    result = store.rollback(target)
    if not result.get("ok"):
        return f"Rollback failed: {result.get('reason', 'unknown error')}"
    reverted = result.get("reverted_files") or []
    summary = f"Rolled the workspace back to checkpoint {result['checkpoint_id']}."
    if reverted:
        preview = ", ".join(f"{item['status']} {item['path']}" for item in reverted[:8])
        more = "" if len(reverted) <= 8 else f" (+{len(reverted) - 8} more)"
        summary += f" Reverted {len(reverted)} file(s): {preview}{more}"
    else:
        summary += " No tracked files had changed since that checkpoint."
    return summary


def _ls(toolbox: Any, path: str) -> str:
    result = toolbox.execute("list_files", {"path": path or "."})
    if not result.ok:
        return result.output
    try:
        rows = json.loads(result.output)
    except Exception:
        return result.output
    if not rows:
        return f"{path or '.'} is empty."
    lines = [f"{path or '.'} ({len(rows)} entries):"]
    for row in rows:
        if row.get("type") == "dir":
            lines.append(f"  {row.get('name')}/")
        else:
            size = row.get("size")
            lines.append(f"  {row.get('name')}" + (f"  ({size} bytes)" if size is not None else ""))
    return "\n".join(lines)


def _tree(toolbox: Any, path: str) -> str:
    result = toolbox.execute("tree", {"path": path or ".", "max_depth": 3})
    return result.output if result.ok else result.output


def _shell_split(args: list[str]) -> tuple[set[str], list[str]]:
    """Split shell-style tokens into flags and paths. '-' alone is a path."""
    flags = {arg for arg in args if arg.startswith("-") and len(arg) > 1}
    paths = [arg for arg in args if not arg.startswith("-") or arg == "-"]
    return flags, paths


def _has_recursive_flag(flags: set[str]) -> bool:
    if "--recursive" in flags:
        return True
    return any(flag.startswith("-") and not flag.startswith("--") and ("r" in flag or "R" in flag) for flag in flags)


def _cat(toolbox: Any, args: list[str]) -> str:
    _, paths = _shell_split(args)
    if not paths:
        return "Usage: /cat <file>"
    outputs = []
    for path in paths:
        result = toolbox.execute("read_file", {"path": path})
        outputs.append(result.output if len(paths) == 1 else f"==> {path} <==\n{result.output}")
    return "\n\n".join(outputs)


def _mkdir(toolbox: Any, args: list[str]) -> str:
    _, paths = _shell_split(args)  # -p is implied; parents are always created
    if not paths:
        return "Usage: /mkdir <dir ...>"
    lines = []
    for path in paths:
        result = toolbox.execute("make_dir", {"path": path})
        lines.append(result.output)
    return "\n".join(lines)


def _touch(toolbox: Any, args: list[str]) -> str:
    _, paths = _shell_split(args)
    if not paths:
        return "Usage: /touch <file ...>"
    lines = []
    for path in paths:
        # append of nothing creates the file without truncating an existing one
        result = toolbox.execute("append_file", {"path": path, "content": ""})
        lines.append(f"Touched {path}" if result.ok else result.output)
    return "\n".join(lines)


def _cp(toolbox: Any, args: list[str]) -> str:
    flags, paths = _shell_split(args)
    if len(paths) != 2:
        return "Usage: /cp [-r] <source> <destination>"
    result = toolbox.execute("copy_path", {
        "source": paths[0],
        "destination": paths[1],
        "recursive": _has_recursive_flag(flags),
    })
    return result.output


def _mv(toolbox: Any, args: list[str]) -> str:
    _, paths = _shell_split(args)
    if len(paths) != 2:
        return "Usage: /mv <source> <destination>"
    result = toolbox.execute("move_path", {"source": paths[0], "destination": paths[1], "overwrite": False})
    return result.output


def _rm(toolbox: Any, args: list[str]) -> str:
    flags, paths = _shell_split(args)
    if not paths:
        return "Usage: /rm [-r] <path ...>"
    recursive = _has_recursive_flag(flags)
    lines = []
    for path in paths:
        result = toolbox.execute("delete_path", {"path": path, "recursive": recursive})
        lines.append(result.output)
    return "\n".join(lines)


def _grep(toolbox: Any, args: list[str]) -> str:
    if not args:
        return "Usage: /grep <pattern> [path]"
    pattern = args[0]
    path = args[1] if len(args) > 1 else "."
    result = toolbox.execute("grep", {"path": path, "pattern": pattern, "case_sensitive": False, "max_results": 100})
    if not result.ok:
        return result.output
    try:
        rows = json.loads(result.output)
    except Exception:
        return result.output
    if not rows:
        return f"No matches for '{pattern}' under {path}."
    lines = [f"{row.get('path')}:{row.get('line')}: {str(row.get('text') or '').strip()}" for row in rows]
    return f"{len(rows)} match(es) for '{pattern}':\n" + "\n".join(lines)


def _find(toolbox: Any, args: list[str]) -> str:
    if not args:
        return "Usage: /find <glob> [path], e.g. /find *.py src"
    pattern = args[0]
    path = args[1] if len(args) > 1 else "."
    result = toolbox.execute("search_files", {"path": path, "pattern": pattern, "max_results": 200})
    if not result.ok:
        return result.output
    try:
        rows = json.loads(result.output)
    except Exception:
        return result.output
    if not rows:
        return f"No files matching '{pattern}' under {path}."
    return f"{len(rows)} file(s) matching '{pattern}':\n" + "\n".join(rows)


def _model(args: list[str]) -> str:
    catalog = model_catalog()
    providers = catalog.get("providers", []) if isinstance(catalog, dict) else []
    engine = default_engine()
    if not args:
        lines = [f"Current engine: {engine.get('provider')}:{engine.get('model')}", "", "Available models:"]
        for item in providers:
            models = item.get("models") or []
            if models:
                lines.append(f"- {item.get('id')}: " + ", ".join(str(m) for m in models[:12]))
        lines.append("")
        lines.append("Switch with /model <model> or /model <provider> <model>.")
        return "\n".join(lines)

    if len(args) >= 2:
        provider, model = args[0].strip().lower(), args[1].strip()
    else:
        model = args[0].strip()
        provider = ""
        for item in providers:
            if model in (item.get("models") or []):
                provider = str(item.get("id"))
                break
        if not provider:
            available = ", ".join(
                str(m) for item in providers for m in (item.get("models") or [])
            )
            return f"Unknown model '{model}'. Specify the provider (/model <provider> {model}) or pick one of: {available}"

    try:
        result = save_engine(provider, model)
    except ValueError as exc:
        return f"Could not switch model: {exc}"
    return f"Engine switched to {result.get('provider')}:{result.get('model')}. New runs in every terminal will use it."


def _tools(toolbox: Any, category: str) -> str:
    catalog = toolbox.describe()
    grouped: dict[str, list[str]] = {}
    for item in catalog:
        grouped.setdefault(str(item.get("category")), []).append(str(item.get("name")))
    if category:
        names = grouped.get(category)
        if not names:
            return f"No tools in category '{category}'. Categories: {', '.join(sorted(grouped))}"
        return f"{category} ({len(names)}): " + ", ".join(sorted(names))
    lines = [f"{len(catalog)} tools:"]
    for key in sorted(grouped):
        lines.append(f"- {key} ({len(grouped[key])}): " + ", ".join(sorted(grouped[key])))
    return "\n".join(lines)


def _access(args: list[str]) -> str:
    action = args[0].lower() if args else "status"
    if action == "grant":
        minutes = computer_access.DEFAULT_GRANT_MINUTES
        if len(args) > 1:
            try:
                minutes = float(args[1])
            except ValueError:
                return "Usage: /access grant [minutes]"
        return _access_text(computer_access.grant(minutes), "Granted timed computer control.")
    if action == "revoke":
        return _access_text(computer_access.revoke(), "Revoked computer control.")
    if action in {"full", "ask"}:
        return _access_text(computer_access.set_mode(action), f"Computer-control mode set to {action}.")
    if action == "status":
        return _access_text(computer_access.status(), "")
    return "Usage: /access [grant [minutes] | revoke | full | ask]"


def _access_text(status: dict, prefix: str) -> str:
    if status["mode"] == "full":
        state = "full control - agents may capture the screen and send input without asking"
    elif status["allowed"]:
        state = f"ask mode - grant active for {max(1, status['seconds_remaining'] // 60)} more min"
    else:
        state = "ask mode - computer control blocked until you grant access"
    policy = (
        "Policy: this mode only governs computer control (screen capture, click, type, keys, "
        "scroll, window focus). The workspace sandbox, destructive-command blocklist, read-only "
        "SQL, and URL validation stay enforced in every mode."
    )
    lines = [line for line in [prefix, state, policy] if line]
    return "\n".join(lines)
