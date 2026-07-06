from __future__ import annotations

import json
import threading
from datetime import datetime, timezone
from pathlib import Path

from flask import Flask, abort, jsonify, render_template, request, send_from_directory
from flask_cors import CORS
from werkzeug.utils import secure_filename

from . import config
from .config import Settings, masked_runtime_config
from .db import Database
from .services.agent_runner import AgentRunner
from .services.agent_identity import agent_label, replace_agent_refs
from .services.evals import EvalService
from .services.models import model_catalog
from .services.names import generate_agent_name
from .services.provider_runtime import default_engine, refresh_provider_models, reveal_provider_api_key, save_engine, save_provider_config, seed_provider_settings_from_env
from .services.runtime import pick_workspace_dir, set_workspace_dir, workspace_status
from .services import computer_access, slash_commands
from .tools.base import parse_scope_paths


import sys

# The built React UI is a read-only bundled resource: from the PyInstaller
# bundle when packaged, from the repo checkout otherwise.
_RESOURCE_ROOT = Path(getattr(sys, "_MEIPASS", "")) if getattr(sys, "frozen", False) else Path(__file__).resolve().parents[1]
ROOT_DIR = _RESOURCE_ROOT
FRONTEND_DIST_DIR = ROOT_DIR / "frontend" / "dist"
FRONTEND_ASSETS_DIR = FRONTEND_DIST_DIR / "assets"


def create_app() -> Flask:
    app = Flask(__name__)
    app.secret_key = Settings.secret_key
    # Bounds drag-and-drop uploads (and every other request body).
    app.config["MAX_CONTENT_LENGTH"] = 50 * 1024 * 1024
    CORS(app)
    db = Database()
    db.init_schema()
    seed_provider_settings_from_env()
    _recover_interrupted_runs(db)
    runner = AgentRunner(db)
    eval_service = EvalService(db)

    @app.get("/")
    def index():
        if not (FRONTEND_DIST_DIR / "index.html").is_file():
            abort(503, description="React frontend is not built. Run `npm run build` in frontend/ first.")
        return send_from_directory(FRONTEND_DIST_DIR, "index.html")

    @app.get("/assets/<path:filename>")
    def frontend_assets(filename: str):
        return send_from_directory(FRONTEND_ASSETS_DIR, filename)

    @app.get("/logo.png")
    def brand_logo():
        # Vite copies public/logo.png to the dist root; serve it as the tab
        # icon in production (dev is handled by Vite's own public dir).
        return send_from_directory(FRONTEND_DIST_DIR, "logo.png", max_age=86400)

    @app.get("/favicon.ico")
    def favicon():
        return send_from_directory(FRONTEND_DIST_DIR, "logo.png", max_age=86400)

    @app.get("/legacy")
    def legacy_index():
        return render_template("index.html")

    @app.get("/api/artifacts/<path:filename>")
    def api_artifact_file(filename: str):
        # Serves screenshots and other run artifacts to the UI (chat thumbnails,
        # gallery). send_from_directory jails the path to the artifacts root;
        # artifact names are timestamped, so cached responses never go stale.
        return send_from_directory(str(config.ARTIFACTS_DIR), filename, max_age=3600)

    @app.post("/api/uploads")
    def api_upload_files():
        # Drag-and-drop attachments land as real workspace files under
        # uploads/, so agents can use their normal file tools on them.
        storages = [item for item in request.files.getlist("files") if item and item.filename]
        if not storages:
            return jsonify({"error": "files_required"}), 400
        if len(storages) > 8:
            return jsonify({"error": "too_many_files", "max": 8}), 400
        uploads_root = Path(workspace_status()["workspace_dir"]) / "uploads"
        uploads_root.mkdir(parents=True, exist_ok=True)
        saved = []
        for storage in storages:
            clean = secure_filename(storage.filename or "") or "upload.bin"
            stem = Path(clean).stem or "upload"
            suffix = Path(clean).suffix
            target = uploads_root / clean
            counter = 1
            while target.exists():
                target = uploads_root / f"{stem}_{counter}{suffix}"
                counter += 1
            storage.save(target)
            saved.append({
                "name": target.name,
                "relative_path": f"uploads/{target.name}",
                "size": target.stat().st_size,
            })
        return jsonify({"files": saved})

    @app.get("/api/config")
    def api_config():
        return jsonify(masked_runtime_config())

    @app.get("/api/models")
    def api_models():
        return jsonify(model_catalog())

    @app.post("/api/engine")
    def api_engine_set():
        data = request.get_json(silent=True) or {}
        provider = str(data.get("provider") or "").strip()
        model = str(data.get("model") or "").strip()
        if not provider or not model:
            return jsonify({"error": "provider_and_model_required"}), 400
        return jsonify({"engine": save_engine(provider, model), "model_catalog": model_catalog()})

    @app.post("/api/providers/<provider>")
    def api_provider_set(provider: str):
        data = request.get_json(silent=True) or {}
        try:
            public = save_provider_config(provider, data)
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
        return jsonify({"provider": public, "model_catalog": model_catalog()})

    @app.post("/api/providers/<provider>/api-key/reveal")
    def api_provider_api_key_reveal(provider: str):
        try:
            return jsonify(reveal_provider_api_key(provider))
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400

    @app.post("/api/providers/<provider>/models/refresh")
    def api_provider_models_refresh(provider: str):
        data = request.get_json(silent=True) or {}
        try:
            result = refresh_provider_models(provider, data)
        except Exception as exc:
            return jsonify({"error": str(exc), "error_type": type(exc).__name__}), 400
        return jsonify({"result": result, "model_catalog": model_catalog()})

    @app.get("/api/health")
    def api_health():
        return jsonify({
            "ok": True,
            "config": masked_runtime_config(),
            "workspace": workspace_status(),
            "metrics": _metrics(db),
        })

    @app.get("/api/state")
    def api_state():
        agents = db.fetchall("SELECT * FROM agents ORDER BY id ASC")
        agent_names = _agent_names(agents)
        runs = db.fetchall(
            "SELECT r.*, a.name AS agent_name, a.title AS agent_title "
            "FROM runs r LEFT JOIN agents a ON a.id=r.agent_id "
            "ORDER BY r.id DESC LIMIT 20"
        )
        eval_runs = db.fetchall("SELECT * FROM eval_runs ORDER BY id DESC LIMIT 10")
        context = db.fetchall(
            "SELECT c.*, a.name AS source_agent_name, a.title AS source_agent_title "
            "FROM shared_context c LEFT JOIN agents a ON a.id=c.source_agent_id "
            "ORDER BY c.id DESC LIMIT 30"
        )
        for item in context:
            item["display_content"] = replace_agent_refs(item.get("content"), agent_names)
            item["source_agent_label"] = agent_label(item.get("source_agent_id"), {
                "name": item.get("source_agent_name"),
                "title": item.get("source_agent_title"),
            })
        metrics = _metrics(db)
        return jsonify({
            "agents": agents,
            "runs": runs,
            "eval_runs": eval_runs,
            "shared_context": context,
            "metrics": metrics,
            "config": masked_runtime_config(),
            "workspace": workspace_status(),
            "model_catalog": model_catalog(),
            "tools": runner.toolbox.describe(),
        })

    @app.get("/api/workspace")
    def api_workspace_get():
        return jsonify(workspace_status())

    @app.post("/api/workspace")
    def api_workspace_set():
        data = request.get_json(silent=True) or {}
        try:
            set_workspace_dir(str(data.get("path") or ""), bool(data.get("create", True)))
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
        return jsonify(workspace_status())

    @app.post("/api/workspace/pick")
    def api_workspace_pick():
        data = request.get_json(silent=True) or {}
        try:
            selected = pick_workspace_dir(str(data.get("initial") or ""))
        except RuntimeError as exc:
            return jsonify({"error": str(exc)}), 501
        if not selected:
            return jsonify({"cancelled": True, "workspace": workspace_status()})
        try:
            set_workspace_dir(selected, True)
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
        return jsonify({"cancelled": False, "workspace": workspace_status()})

    @app.get("/api/computer-access")
    def api_computer_access_get():
        return jsonify(computer_access.status())

    @app.post("/api/computer-access/mode")
    def api_computer_access_mode():
        data = request.get_json(silent=True) or {}
        try:
            return jsonify(computer_access.set_mode(str(data.get("mode") or "")))
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400

    @app.post("/api/computer-access/grant")
    def api_computer_access_grant():
        data = request.get_json(silent=True) or {}
        try:
            minutes = float(data.get("minutes") or computer_access.DEFAULT_GRANT_MINUTES)
        except (TypeError, ValueError):
            return jsonify({"error": "minutes must be a number"}), 400
        return jsonify(computer_access.grant(minutes))

    @app.post("/api/computer-access/revoke")
    def api_computer_access_revoke():
        return jsonify(computer_access.revoke())

    @app.post("/api/workspace/open-vscode")
    def api_workspace_open_vscode():
        data = request.get_json(silent=True) or {}
        result = runner.toolbox.execute("open_vscode", {"path": str(data.get("path") or ".")})
        status = 200 if result.ok else 500
        return jsonify({"ok": result.ok, "output": result.output, "meta": result.meta}), status

    @app.post("/api/agents")
    def api_create_agent():
        data = request.get_json(silent=True) or {}
        existing = {row["name"] for row in db.fetchall("SELECT name FROM agents")}
        name, title = generate_agent_name(existing)
        name = data.get("name") or name
        title = data.get("title") or title
        engine = default_engine()
        provider = data.get("provider") or engine["provider"]
        model = data.get("model") or engine["model"]
        system_prompt = str(data.get("system_prompt") or "").strip()
        scope = parse_scope_paths(data.get("scope_paths"))
        agent_id = db.execute(
            "INSERT INTO agents (name, title, status, provider, model, system_prompt, scope_paths) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (name, title, "idle", provider, model, system_prompt, json.dumps(scope) if scope else ""),
        )
        return jsonify({"id": agent_id, "name": name, "title": title})

    @app.delete("/api/agents")
    def api_delete_all_agents():
        running = db.fetchall("SELECT id, name, title FROM agents WHERE status=? ORDER BY id ASC", ("running",))
        if running:
            return jsonify({"error": "agents_running", "running": running}), 409

        agents = db.fetchall("SELECT id FROM agents ORDER BY id ASC")
        summary = _delete_agents(db, [int(agent["id"]) for agent in agents])
        return jsonify(summary)

    @app.delete("/api/agents/<int:agent_id>")
    def api_delete_agent(agent_id: int):
        agent = db.fetchone("SELECT * FROM agents WHERE id=?", (agent_id,))
        if not agent:
            return jsonify({"error": "agent_not_found"}), 404
        if agent.get("status") == "running":
            return jsonify({"error": "agent_running", "agent": agent}), 409

        summary = _delete_agents(db, [agent_id])
        return jsonify(summary)

    @app.post("/api/agents/<int:agent_id>/clear")
    def api_clear_agent(agent_id: int):
        agent = db.fetchone("SELECT * FROM agents WHERE id=?", (agent_id,))
        if not agent:
            return jsonify({"error": "agent_not_found"}), 404
        if agent.get("status") == "running":
            return jsonify({"error": "agent_running", "agent": agent}), 409

        return jsonify(_clear_agent_records(db, agent_id, reset_status=True))

    @app.post("/api/agents/<int:agent_id>/profile")
    def api_agent_profile(agent_id: int):
        agent = db.fetchone("SELECT * FROM agents WHERE id=?", (agent_id,))
        if not agent:
            return jsonify({"error": "agent_not_found"}), 404
        data = request.get_json(silent=True) or {}
        updates: list[tuple[str, str]] = []
        if "title" in data:
            updates.append(("title", str(data.get("title") or "").strip()))
        if "system_prompt" in data:
            updates.append(("system_prompt", str(data.get("system_prompt") or "").strip()))
        if "scope_paths" in data:
            scope = parse_scope_paths(data.get("scope_paths"))
            updates.append(("scope_paths", json.dumps(scope)))
        if not updates:
            return jsonify({"error": "nothing_to_update", "allowed": ["title", "system_prompt", "scope_paths"]}), 400
        assignments = ", ".join(f"{column}=?" for column, _ in updates)
        db.execute(f"UPDATE agents SET {assignments} WHERE id=?", (*[value for _, value in updates], agent_id))
        return jsonify({"agent": db.fetchone("SELECT * FROM agents WHERE id=?", (agent_id,))})

    @app.get("/api/agents/<int:agent_id>/messages")
    def api_agent_messages(agent_id: int):
        # Newest 200, presented chronologically - the old oldest-200 window
        # froze long-lived terminals on their earliest messages forever.
        messages = list(reversed(db.fetchall(
            "SELECT * FROM messages WHERE agent_id=? ORDER BY id DESC LIMIT 200", (agent_id,)
        )))
        activities = _agent_activities(db, agent_id)
        latest = db.fetchone("SELECT id FROM runs WHERE agent_id=? ORDER BY id DESC LIMIT 1", (agent_id,))
        plans = []
        if latest:
            plans = db.fetchall("SELECT * FROM plans WHERE run_id=? ORDER BY sort_order ASC", (latest["id"],))
        return jsonify({"messages": messages, "plans": plans, "activities": activities})

    @app.post("/api/agents/<int:agent_id>/message")
    def api_agent_message(agent_id: int):
        data = request.get_json(silent=True) or {}
        content = (data.get("content") or "").strip()
        if not content:
            return jsonify({"error": "content_required"}), 400
        agent = db.fetchone("SELECT * FROM agents WHERE id=?", (agent_id,))
        if not agent:
            return jsonify({"error": "agent_not_found"}), 404
        if slash_commands.is_command(content):
            command_name = content.strip().split()[0].lstrip("/").lower()
            if command_name in {"clear", "reset"}:
                # /clear owns the message history, so it cannot go through the
                # generic handler (which would immediately re-insert messages).
                busy_run = db.fetchone("SELECT id FROM runs WHERE agent_id=? AND status=? ORDER BY id DESC LIMIT 1", (agent_id, "running"))
                if agent.get("status") == "running" and busy_run:
                    reply = "Cannot clear while this terminal is running. Wait for the run to finish, then try /clear again."
                    return _command_exchange(db, agent_id, content, reply, cleared=False)
                summary = _clear_agent_records(db, agent_id, reset_status=True)
                return jsonify({"status": "handled", "command": True, "cleared": True, "reply": "Chat cleared.", "summary": summary})
            if command_name in {"role", "scope"}:
                # Agent-specific commands: they edit THIS terminal's profile,
                # so they cannot go through the agent-agnostic handler.
                reply = _handle_profile_command(db, agent_id, agent, content)
                return _command_exchange(db, agent_id, content, reply)
            reply = slash_commands.handle(runner.toolbox, content)
            return _command_exchange(db, agent_id, content, reply)
        running_run = db.fetchone("SELECT id FROM runs WHERE agent_id=? AND status=? ORDER BY id DESC LIMIT 1", (agent_id, "running"))
        if agent.get("status") == "running" and running_run:
            current_agent_name = agent_label(agent_id, agent)
            message_id = db.execute(
                "INSERT INTO messages (agent_id, role, content, run_id) VALUES (?, ?, ?, ?)",
                (agent_id, "user", content, None),
            )
            context_id = db.execute(
                "INSERT INTO shared_context (source_agent_id, role, content, importance) VALUES (?, ?, ?, ?)",
                (agent_id, "user", f"Live context injected for {current_agent_name}: {content}", 4),
            )
            db.insert_json_event(running_run["id"], "context_injected", {
                "message_id": message_id,
                "agent_name": current_agent_name,
                "content": content,
            })
            return jsonify({"status": "injected", "message_id": message_id, "context_id": context_id})
        if agent.get("status") == "running" and not running_run:
            db.execute("UPDATE agents SET status=? WHERE id=?", ("idle", agent_id))
        provider = data.get("provider")
        model = data.get("model")
        db.execute("UPDATE agents SET status=? WHERE id=?", ("running", agent_id))

        def worker():
            runner.run_turn_resilient(agent_id, content, provider, model)

        thread = threading.Thread(target=worker, daemon=True)
        thread.start()
        return jsonify({"status": "queued"})

    @app.post("/api/agents/<int:agent_id>/stop")
    def api_stop_agent(agent_id: int):
        agent = db.fetchone("SELECT * FROM agents WHERE id=?", (agent_id,))
        if not agent:
            return jsonify({"error": "agent_not_found"}), 404
        run = runner.request_stop(agent_id)
        if run:
            # Cooperative stop: the loop ends the run at its next checkpoint;
            # a tool or model call already in flight finishes first.
            return jsonify({"status": "stopping", "run_id": run["id"]})
        if agent.get("status") == "running":
            # Status says running but no live run exists (stale row after a
            # backend restart): reset so the terminal is usable again.
            db.execute("UPDATE agents SET status=? WHERE id=?", ("idle", agent_id))
            return jsonify({"status": "reset"})
        return jsonify({"error": "not_running"}), 409

    @app.post("/api/runs/<int:run_id>/rollback")
    def api_run_rollback(run_id: int):
        run = db.fetchone("SELECT id, agent_id, checkpoint_id FROM runs WHERE id=?", (run_id,))
        if not run:
            return jsonify({"error": "run_not_found"}), 404
        checkpoint_id = str(run.get("checkpoint_id") or "").strip()
        if not checkpoint_id:
            return jsonify({"error": "no_checkpoint", "detail": "This run has no workspace checkpoint to roll back to."}), 400
        # A rollback resets the shared workspace; doing it while an agent is
        # running would wipe that agent's in-flight edits out from under it.
        busy = db.fetchone("SELECT COUNT(*) AS n FROM runs WHERE status=?", ("running",))
        if busy and int(busy.get("n") or 0) > 0:
            return jsonify({"error": "agents_running", "detail": "Stop or wait for running terminals before rolling back."}), 409
        result = runner.checkpoints.rollback(checkpoint_id)
        if not result.get("ok"):
            return jsonify({"error": "rollback_failed", "detail": result.get("reason")}), 400
        db.insert_json_event(run_id, "rollback", {
            "checkpoint_id": checkpoint_id,
            "reverted_files": result.get("reverted_files", []),
        })
        return jsonify({"status": "rolled_back", **result})

    @app.get("/api/runs/<int:run_id>")
    def api_run(run_id: int):
        run = db.fetchone(
            "SELECT r.*, a.name AS agent_name, a.title AS agent_title "
            "FROM runs r LEFT JOIN agents a ON a.id=r.agent_id WHERE r.id=?",
            (run_id,),
        )
        events = db.fetchall("SELECT * FROM run_events WHERE run_id=? ORDER BY id ASC", (run_id,))
        plans = db.fetchall("SELECT * FROM plans WHERE run_id=? ORDER BY sort_order ASC", (run_id,))
        for event in events:
            try:
                event["payload"] = json.loads(event.pop("payload_json"))
            except Exception:
                event["payload"] = {}
        return jsonify({"run": run, "events": events, "plans": plans})

    @app.post("/api/context")
    def api_context():
        data = request.get_json(silent=True) or {}
        content = (data.get("content") or "").strip()
        if not content:
            return jsonify({"error": "content_required"}), 400
        context_id = db.execute(
            "INSERT INTO shared_context (source_agent_id, role, content, importance) VALUES (?, ?, ?, ?)",
            (None, "user", content, 3),
        )
        return jsonify({"id": context_id})

    @app.post("/api/evals/run")
    def api_run_eval():
        data = request.get_json(silent=True) or {}
        if data.get("wait"):
            eval_id = eval_service.run_eval(data.get("provider"), data.get("model"))
            return jsonify({"status": "complete", "id": eval_id})

        def worker():
            eval_service.run_eval(data.get("provider"), data.get("model"))

        thread = threading.Thread(target=worker, daemon=True)
        thread.start()
        return jsonify({"status": "queued"})

    @app.get("/api/evals/<int:eval_id>")
    def api_eval(eval_id: int):
        eval_run = db.fetchone("SELECT * FROM eval_runs WHERE id=?", (eval_id,))
        items = db.fetchall("SELECT * FROM eval_items WHERE eval_run_id=? ORDER BY id ASC", (eval_id,))
        return jsonify({"eval": eval_run, "items": items})

    return app


def _command_exchange(db: Database, agent_id: int, content: str, reply: str, **extra):
    """Record a slash command's user line + assistant reply and return the
    standard 'handled' response. One shape for every instant command."""
    message_id = db.execute(
        "INSERT INTO messages (agent_id, role, content, run_id) VALUES (?, ?, ?, ?)",
        (agent_id, "user", content, None),
    )
    reply_id = db.execute(
        "INSERT INTO messages (agent_id, role, content, run_id) VALUES (?, ?, ?, ?)",
        (agent_id, "assistant", reply, None),
    )
    return jsonify({
        "status": "handled",
        "command": True,
        "reply": reply,
        "message_id": message_id,
        "reply_message_id": reply_id,
        **extra,
    })


def _handle_profile_command(db: Database, agent_id: int, agent: dict, content: str) -> str:
    """Terminal-native role management: /role <instructions> and /scope <globs>.

    Roles turn a terminal into a durable specialist (backend, security, QA...);
    scopes are deterministic write boundaries enforced by the file tools."""
    parts = content.strip().split(None, 1)
    command = parts[0].lstrip("/").lower()
    remainder = parts[1].strip() if len(parts) > 1 else ""
    if command == "role":
        if not remainder:
            current = str(agent.get("system_prompt") or "").strip()
            return f"Current role:\n{current}" if current else (
                "No role set. Use /role <instructions> to make this agent a specialist "
                "(e.g. /role You are the backend agent: REST API and database only; never touch UI files). "
                "Use /role clear to remove it."
            )
        if remainder.lower() == "clear":
            db.execute("UPDATE agents SET system_prompt=? WHERE id=?", ("", agent_id))
            return "Role cleared. This agent is a generalist again."
        db.execute("UPDATE agents SET system_prompt=? WHERE id=?", (remainder, agent_id))
        return f"Role set. From the next run, {agent.get('name') or 'this agent'} works as:\n{remainder}"
    if not remainder:
        scope = parse_scope_paths(agent.get("scope_paths"))
        return f"Current write scope (enforced): {', '.join(scope)}" if scope else (
            "No write scope set - this agent can write anywhere in the workspace. "
            "Use /scope <glob ...> to fence it in (e.g. /scope server/* shared/*). Use /scope clear to remove it."
        )
    if remainder.lower() == "clear":
        db.execute("UPDATE agents SET scope_paths=? WHERE id=?", ("", agent_id))
        return "Write scope cleared. This agent can write anywhere in the workspace."
    globs = parse_scope_paths(remainder)
    db.execute("UPDATE agents SET scope_paths=? WHERE id=?", (json.dumps(globs), agent_id))
    return (
        f"Write scope set (enforced from the next run): {', '.join(globs)}. "
        "Writes outside these paths will be blocked; reading stays workspace-wide."
    )


def _recover_interrupted_runs(db: Database) -> None:
    stamp = datetime.now(timezone.utc).replace(tzinfo=None).isoformat(sep=" ", timespec="seconds")
    running = db.fetchall("SELECT id FROM runs WHERE status=?", ("running",))
    for run in running:
        db.insert_json_event(run["id"], "run_error", {
            "error": "Run was interrupted by backend restart.",
            "error_type": "InterruptedRun",
        })
    db.execute(
        "UPDATE runs SET status=?, error_text=?, ended_at=? WHERE status=?",
        ("error", "Run was interrupted by backend restart.", stamp, "running"),
    )
    db.execute("UPDATE agents SET status=? WHERE status=?", ("idle", "running"))


def _delete_agents(db: Database, agent_ids: list[int]) -> dict:
    clean_ids = sorted({int(agent_id) for agent_id in agent_ids if int(agent_id) > 0})
    summary = {
        "deleted_agents": clean_ids,
        "deleted_messages": 0,
        "deleted_runs": 0,
        "deleted_run_events": 0,
        "deleted_plans": 0,
        "deleted_shared_context": 0,
    }
    for agent_id in clean_ids:
        cleared = _clear_agent_records(db, agent_id, reset_status=False)
        summary["deleted_messages"] += cleared["deleted_messages"]
        summary["deleted_runs"] += cleared["deleted_runs"]
        summary["deleted_run_events"] += cleared["deleted_run_events"]
        summary["deleted_plans"] += cleared["deleted_plans"]
        summary["deleted_shared_context"] += cleared["deleted_shared_context"]
        _delete_count(db, "agents", "id", agent_id)
    return summary


def _clear_agent_records(db: Database, agent_id: int, *, reset_status: bool) -> dict:
    clean_id = int(agent_id)
    summary = {
        "cleared_agent": clean_id,
        "deleted_messages": 0,
        "deleted_runs": 0,
        "deleted_run_events": 0,
        "deleted_plans": 0,
        "deleted_shared_context": 0,
    }
    runs = db.fetchall("SELECT id FROM runs WHERE agent_id=?", (clean_id,))
    run_ids = [int(row["id"]) for row in runs]
    for run_id in run_ids:
        summary["deleted_plans"] += _delete_count(db, "plans", "run_id", run_id)
        summary["deleted_run_events"] += _delete_count(db, "run_events", "run_id", run_id)
    summary["deleted_runs"] += _delete_count(db, "runs", "agent_id", clean_id)
    summary["deleted_messages"] += _delete_count(db, "messages", "agent_id", clean_id)
    summary["deleted_shared_context"] += _delete_count(db, "shared_context", "source_agent_id", clean_id)
    if reset_status:
        stamp = datetime.now(timezone.utc).replace(tzinfo=None).isoformat(sep=" ", timespec="seconds")
        db.execute("UPDATE agents SET status=?, updated_at=? WHERE id=?", ("idle", stamp, clean_id))
    return summary


def _delete_count(db: Database, table: str, column: str, value: int) -> int:
    row = db.fetchone(f"SELECT COUNT(*) AS total FROM {table} WHERE {column}=?", (value,))
    total = int(row.get("total") or 0) if row else 0
    db.execute(f"DELETE FROM {table} WHERE {column}=?", (value,))
    return total


def _metrics(db: Database) -> dict:
    totals = db.fetchone(
        "SELECT COUNT(*) AS total, SUM(CASE WHEN status='complete' THEN 1 ELSE 0 END) AS complete, "
        "AVG(NULLIF(latency_ms,0)) AS avg_latency, SUM(tool_count) AS tools, SUM(token_estimate) AS tokens FROM runs"
    ) or {}
    latest_eval = db.fetchone("SELECT * FROM eval_runs ORDER BY id DESC LIMIT 1")
    category_rows = db.fetchall(
        "SELECT category, AVG(score) AS avg_score, COUNT(*) AS total FROM eval_items "
        "WHERE eval_run_id=(SELECT id FROM eval_runs ORDER BY id DESC LIMIT 1) GROUP BY category"
    )
    return {
        "runs_total": int(totals.get("total") or 0),
        "runs_complete": int(totals.get("complete") or 0),
        "avg_latency_ms": round(float(totals.get("avg_latency") or 0), 1),
        "tool_calls": int(totals.get("tools") or 0),
        "token_estimate": int(totals.get("tokens") or 0),
        "latest_eval": latest_eval,
        "category_scores": category_rows,
        "now": datetime.now(timezone.utc).replace(tzinfo=None).isoformat(),
    }


def _agent_names(agents: list[dict]) -> dict[int, str]:
    names: dict[int, str] = {}
    for row in agents:
        try:
            current_id = int(row.get("id"))
        except Exception:
            continue
        name = str(row.get("name") or "").strip()
        if name:
            names[current_id] = name
    return names


def _agent_activities(db: Database, agent_id: int) -> list[dict]:
    event_types = (
        "run_started",
        "model_response",
        "tool_result",
        "run_error",
        "run_blocked",
        "run_complete",
        "context_injected",
        "plan_progress",
        "recovery_attempt",
        "provider_retry",
        "provider_retry_failed",
        "auto_recovery",
        "auto_recovery_stopped",
        "parse_retry",
        "intent",
        "stop_requested",
        "run_stopped",
        "checkpoint",
        "rollback",
    )
    placeholders = ", ".join("?" for _ in event_types)
    # Newest events, not oldest: ORDER BY ASC LIMIT froze long agents on their
    # first 500 events. Fetch the most recent, then present chronologically.
    rows = db.fetchall(
        "SELECT e.id, e.run_id, e.event_type, e.payload_json, e.created_at "
        "FROM run_events e JOIN runs r ON e.run_id=r.id "
        f"WHERE r.agent_id=? AND e.event_type IN ({placeholders}) "
        "ORDER BY e.id DESC LIMIT 200",
        (agent_id, *event_types),
    )
    activities = []
    for row in reversed(rows):
        try:
            payload = json.loads(row.pop("payload_json") or "{}")
        except Exception:
            payload = {}
        activities.append({
            "id": row["id"],
            "run_id": row["run_id"],
            "type": row["event_type"],
            "payload": _trim_activity_payload(payload),
            "created_at": row["created_at"],
        })
    return activities


def _trim_activity_payload(payload: dict) -> dict:
    # The activity feed only needs previews; full text lives in messages and
    # the run artifact. Untrimmed model_response text and tool_result output
    # (web scrapes, file reads) accumulated into a browser out-of-memory crash
    # because every 4s poll re-fetched and re-rendered them.
    if not isinstance(payload, dict):
        return {}
    trimmed = dict(payload)
    for key, limit in (("text", 2000), ("output", 2000), ("reason", 1000), ("error", 1000)):
        value = trimmed.get(key)
        if isinstance(value, str) and len(value) > limit:
            trimmed[key] = value[:limit] + "... [truncated]"
    return trimmed
