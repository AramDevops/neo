from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Dict, Optional


# When packaged as a single executable (PyInstaller), writable data - the .env,
# the sqlite db, artifacts and the workspace - lives next to the exe, not inside
# the read-only bundle. In a normal checkout it is the repo root as before.
FROZEN = bool(getattr(sys, "frozen", False))
ROOT = Path(sys.executable).resolve().parent if FROZEN else Path(__file__).resolve().parents[1]
ENV_PATH = ROOT / ".env"
ARTIFACTS_DIR = ROOT / "artifacts"
WORKSPACE_DIR = ROOT / "workspace"


def _load_dotenv(path: Path = ENV_PATH) -> Dict[str, str]:
    values: Dict[str, str] = {}
    if not path.exists():
        return values
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        values[key] = value
        os.environ.setdefault(key, value)
    return values


_ENV = _load_dotenv()


def env(name: str, default: Optional[str] = None) -> Optional[str]:
    return os.environ.get(name, _ENV.get(name, default))


def neo_env(name: str, default: Optional[str] = None) -> Optional[str]:
    return env(name, default)


class Settings:
    app_name = "Neo"
    secret_key = neo_env("NEO_SECRET_KEY", "dev-local-neo")
    provider = neo_env("NEO_PROVIDER", "gemini")
    model = neo_env("NEO_MODEL", "gemini-3.5-flash")
    # Flash-tier models emit ONE tool call per response, so a create-app task
    # (scaffold files + install + start + verify) needs more loops than a
    # batch-capable model would. Run 65 exhausted 8 loops on file writes alone.
    max_agent_loops = int(neo_env("NEO_MAX_AGENT_LOOPS", "12") or "12")
    # A blocked run is automatically retried this many times with the failure
    # evidence, so a recoverable hiccup (failed start, port issue) self-heals
    # instead of waiting for the user to say "try again". Bounded by both this
    # cap and a no-progress guard (identical blocker twice => stop).
    auto_recovery_max = int(neo_env("NEO_AUTO_RECOVERY_MAX", "2") or "2")
    shell_enabled = (neo_env("NEO_ENABLE_SHELL", "1") or "1") == "1"
    tool_timeout_seconds = int(neo_env("NEO_TOOL_TIMEOUT_SECONDS", "20") or "20")
    # Snapshot the workspace to a shadow git repo before each work run so the
    # user can /rollback a bad run. Best-effort; no-op when git is absent.
    checkpoints_enabled = (neo_env("NEO_ENABLE_CHECKPOINTS", "1") or "1") == "1"

    # A packaged exe defaults to zero-setup sqlite; a dev checkout keeps mysql.
    db_driver = neo_env("NEO_DB_DRIVER", "sqlite" if FROZEN else "mysql")
    mysql_host = neo_env("NEO_MYSQL_HOST", "localhost")
    mysql_port = int(neo_env("NEO_MYSQL_PORT", "3306") or "3306")
    mysql_user = neo_env("NEO_MYSQL_USER", "root")
    mysql_password = neo_env("NEO_MYSQL_PASSWORD", "") or ""
    mysql_database = neo_env("NEO_MYSQL_DATABASE", "neo_harness")
    mysql_charset = neo_env("NEO_MYSQL_CHARSET", "utf8mb4")
    sqlite_path = neo_env("NEO_SQLITE_PATH", str(ROOT / "neo_harness.db"))

    gemini_api_key = env("GEMINI_API_KEY", "")
    openai_api_key = env("OPENAI_API_KEY", "")
    anthropic_api_key = env("ANTHROPIC_API_KEY", env("CLAUDE_API_KEY", ""))
    local_base_url = neo_env("NEO_LOCAL_BASE_URL", "http://127.0.0.1:11434/v1")
    local_api_key = neo_env("NEO_LOCAL_API_KEY", "local")
    local_models = neo_env("NEO_LOCAL_MODELS", "llama3.1,gemma3,qwen2.5-coder")


def masked_runtime_config() -> dict:
    return {
        "provider": Settings.provider,
        "model": Settings.model,
        "db_driver": Settings.db_driver,
        "mysql_host": Settings.mysql_host,
        "mysql_database": Settings.mysql_database,
        "has_gemini_key": bool(Settings.gemini_api_key),
        "has_openai_key": bool(Settings.openai_api_key),
        "has_anthropic_key": bool(Settings.anthropic_api_key),
        "local_base_url": Settings.local_base_url,
        "has_local_api_key": bool(Settings.local_api_key),
        "shell_enabled": Settings.shell_enabled,
        "workspace_dir": str(WORKSPACE_DIR),
        "artifacts_dir": str(ARTIFACTS_DIR),
    }
