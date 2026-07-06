from __future__ import annotations

import json
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

import pymysql

from .config import Settings


# Bounded MySQL connection pool. Opening a fresh TCP connection per query
# exhausts Windows ephemeral ports under UI polling (WinError 10048) and
# produced intermittent 500s. Connections are reused across requests and
# validated with ping(reconnect=True) before use.
_MYSQL_POOL: list = []
_MYSQL_POOL_KEY: tuple | None = None
_MYSQL_POOL_LOCK = threading.Lock()
_MYSQL_POOL_MAX = 8


class Database:
    def __init__(self) -> None:
        self.driver = Settings.db_driver.lower()
        if self.driver not in {"mysql", "sqlite"}:
            self.driver = "mysql"

    def _mysql_connect(self, with_db: bool = True):
        kwargs = {
            "host": Settings.mysql_host,
            "port": Settings.mysql_port,
            "user": Settings.mysql_user,
            "password": Settings.mysql_password,
            "charset": Settings.mysql_charset,
            "cursorclass": pymysql.cursors.DictCursor,
            "autocommit": False,
            "connect_timeout": 3,
            "read_timeout": 10,
            "write_timeout": 10,
        }
        if with_db:
            kwargs["database"] = Settings.mysql_database
        last_error: Exception | None = None
        for attempt in range(4):
            try:
                return pymysql.connect(**kwargs)
            except pymysql.err.OperationalError as exc:
                last_error = exc
                if not self._is_transient_connect_error(exc) or attempt == 3:
                    raise
                time.sleep(0.08 * (attempt + 1))
        raise last_error or RuntimeError("MySQL connection failed")

    def _is_transient_connect_error(self, exc: Exception) -> bool:
        text = " ".join(str(exc).split()).lower()
        return any(term in text for term in [
            "winerror 10048",
            "can't connect to mysql server",
            "too many connections",
            "temporarily unavailable",
            "connection refused",
            "timed out",
        ])

    def _sqlite_connect(self):
        path = Path(Settings.sqlite_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(path), check_same_thread=False)
        conn.row_factory = sqlite3.Row
        return conn

    def _mysql_pool_key(self) -> tuple:
        return (
            Settings.mysql_host,
            Settings.mysql_port,
            Settings.mysql_user,
            Settings.mysql_password,
            Settings.mysql_database,
            Settings.mysql_charset,
        )

    def _acquire_mysql(self):
        global _MYSQL_POOL_KEY
        key = self._mysql_pool_key()
        with _MYSQL_POOL_LOCK:
            if _MYSQL_POOL_KEY != key:
                for stale in _MYSQL_POOL:
                    try:
                        stale.close()
                    except Exception:
                        pass
                _MYSQL_POOL.clear()
                _MYSQL_POOL_KEY = key
            conn = _MYSQL_POOL.pop() if _MYSQL_POOL else None
        if conn is not None:
            try:
                conn.ping(reconnect=True)
                return conn
            except Exception:
                try:
                    conn.close()
                except Exception:
                    pass
        return self._mysql_connect()

    def _release_mysql(self, conn, healthy: bool) -> None:
        if healthy:
            with _MYSQL_POOL_LOCK:
                if _MYSQL_POOL_KEY == self._mysql_pool_key() and len(_MYSQL_POOL) < _MYSQL_POOL_MAX:
                    _MYSQL_POOL.append(conn)
                    return
        try:
            conn.close()
        except Exception:
            pass

    @contextmanager
    def connect(self):
        if self.driver == "mysql":
            conn = self._acquire_mysql()
            healthy = True
            try:
                yield conn
                conn.commit()
            except Exception:
                healthy = False
                try:
                    conn.rollback()
                    healthy = True
                except Exception:
                    pass
                raise
            finally:
                self._release_mysql(conn, healthy)
            return
        conn = self._sqlite_connect()
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def ensure_database(self) -> None:
        if self.driver != "mysql":
            return
        conn = self._mysql_connect(with_db=False)
        try:
            with conn.cursor() as cur:
                cur.execute(
                    f"CREATE DATABASE IF NOT EXISTS `{Settings.mysql_database}` "
                    "CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci"
                )
            conn.commit()
        finally:
            conn.close()

    def q(self, sql: str) -> str:
        if self.driver == "mysql":
            return sql.replace("?", "%s")
        return sql

    def execute(self, sql: str, params: Iterable[Any] = ()) -> int:
        with self.connect() as conn:
            cur = conn.cursor()
            cur.execute(self.q(sql), tuple(params))
            return int(getattr(cur, "lastrowid", 0) or 0)

    def fetchall(self, sql: str, params: Iterable[Any] = ()) -> List[Dict[str, Any]]:
        with self.connect() as conn:
            cur = conn.cursor()
            cur.execute(self.q(sql), tuple(params))
            rows = cur.fetchall()
            return [dict(row) for row in rows]

    def fetchone(self, sql: str, params: Iterable[Any] = ()) -> Optional[Dict[str, Any]]:
        rows = self.fetchall(sql, params)
        return rows[0] if rows else None

    def init_schema(self) -> None:
        self.ensure_database()
        statements = MYSQL_SCHEMA if self.driver == "mysql" else SQLITE_SCHEMA
        with self.connect() as conn:
            cur = conn.cursor()
            for statement in statements:
                cur.execute(statement)
        self._run_migrations()

    def insert_json_event(self, run_id: int, event_type: str, payload: Dict[str, Any]) -> int:
        return self.execute(
            "INSERT INTO run_events (run_id, event_type, payload_json) VALUES (?, ?, ?)",
            (run_id, event_type, json.dumps(payload, ensure_ascii=False, default=str)),
        )

    def _run_migrations(self) -> None:
        self._add_column_if_missing("runs", "artifact_path", "TEXT NULL", "TEXT")
        self._add_column_if_missing("runs", "loop_count", "INT DEFAULT 0", "INTEGER DEFAULT 0")
        self._add_column_if_missing("eval_runs", "error_text", "MEDIUMTEXT NULL", "TEXT")
        self._add_column_if_missing("eval_items", "run_id", "INT NULL", "INTEGER")
        # Role system: a durable per-agent system prompt (role instructions)
        # and an enforced write scope (JSON list of workspace glob patterns).
        self._add_column_if_missing("agents", "system_prompt", "MEDIUMTEXT NULL", "TEXT DEFAULT ''")
        self._add_column_if_missing("agents", "scope_paths", "TEXT NULL", "TEXT DEFAULT ''")
        # The workspace checkpoint taken at this run's start, so a run's edits
        # can be rolled back to their pre-run state.
        self._add_column_if_missing("runs", "checkpoint_id", "VARCHAR(40) NULL", "TEXT DEFAULT ''")

    def _columns_for(self, table: str) -> set[str]:
        if not table.replace("_", "").isalnum():
            raise ValueError(f"Unsafe table name: {table}")
        with self.connect() as conn:
            cur = conn.cursor()
            if self.driver == "mysql":
                cur.execute(f"SHOW COLUMNS FROM `{table}`")
                rows = cur.fetchall()
                return {str(row["Field"]) for row in rows}
            cur.execute(f"PRAGMA table_info({table})")
            return {str(row["name"]) for row in cur.fetchall()}

    def _add_column_if_missing(self, table: str, column: str, mysql_def: str, sqlite_def: str) -> None:
        if column in self._columns_for(table):
            return
        if not column.replace("_", "").isalnum():
            raise ValueError(f"Unsafe column name: {column}")
        definition = mysql_def if self.driver == "mysql" else sqlite_def
        table_name = f"`{table}`" if self.driver == "mysql" else table
        self.execute(f"ALTER TABLE {table_name} ADD COLUMN {column} {definition}")


MYSQL_SCHEMA = [
    """
    CREATE TABLE IF NOT EXISTS agents (
        id INT AUTO_INCREMENT PRIMARY KEY,
        name VARCHAR(120) NOT NULL,
        title VARCHAR(200) DEFAULT '',
        status VARCHAR(40) DEFAULT 'idle',
        provider VARCHAR(80) DEFAULT '',
        model VARCHAR(160) DEFAULT '',
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
        INDEX idx_agents_status (status, id)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
    """,
    """
    CREATE TABLE IF NOT EXISTS messages (
        id INT AUTO_INCREMENT PRIMARY KEY,
        agent_id INT NOT NULL,
        role VARCHAR(40) NOT NULL,
        content MEDIUMTEXT NOT NULL,
        run_id INT NULL,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        INDEX idx_messages_agent (agent_id, id),
        INDEX idx_messages_run (run_id)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
    """,
    """
    CREATE TABLE IF NOT EXISTS shared_context (
        id INT AUTO_INCREMENT PRIMARY KEY,
        source_agent_id INT NULL,
        role VARCHAR(40) NOT NULL,
        content MEDIUMTEXT NOT NULL,
        importance INT DEFAULT 1,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        INDEX idx_context_created (created_at),
        INDEX idx_context_source (source_agent_id, id)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
    """,
    """
    CREATE TABLE IF NOT EXISTS runs (
        id INT AUTO_INCREMENT PRIMARY KEY,
        agent_id INT NULL,
        status VARCHAR(40) NOT NULL,
        provider VARCHAR(80) DEFAULT '',
        model VARCHAR(160) DEFAULT '',
        user_prompt MEDIUMTEXT,
        final_output MEDIUMTEXT,
        error_text MEDIUMTEXT,
        artifact_path TEXT NULL,
        latency_ms INT DEFAULT 0,
        tool_count INT DEFAULT 0,
        loop_count INT DEFAULT 0,
        token_estimate INT DEFAULT 0,
        started_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        ended_at TIMESTAMP NULL,
        INDEX idx_runs_agent (agent_id, id),
        INDEX idx_runs_status (status, id)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
    """,
    """
    CREATE TABLE IF NOT EXISTS run_events (
        id INT AUTO_INCREMENT PRIMARY KEY,
        run_id INT NOT NULL,
        event_type VARCHAR(80) NOT NULL,
        payload_json MEDIUMTEXT NOT NULL,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        INDEX idx_events_run (run_id, id),
        INDEX idx_events_type (event_type, id)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
    """,
    """
    CREATE TABLE IF NOT EXISTS plans (
        id INT AUTO_INCREMENT PRIMARY KEY,
        run_id INT NOT NULL,
        step_text TEXT NOT NULL,
        status VARCHAR(40) DEFAULT 'pending',
        sort_order INT DEFAULT 0,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        INDEX idx_plans_run (run_id, sort_order)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
    """,
    """
    CREATE TABLE IF NOT EXISTS eval_runs (
        id INT AUTO_INCREMENT PRIMARY KEY,
        status VARCHAR(40) NOT NULL,
        provider VARCHAR(80) DEFAULT '',
        model VARCHAR(160) DEFAULT '',
        score FLOAT DEFAULT 0,
        passed INT DEFAULT 0,
        total INT DEFAULT 0,
        latency_ms INT DEFAULT 0,
        summary_json MEDIUMTEXT,
        error_text MEDIUMTEXT NULL,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        ended_at TIMESTAMP NULL,
        INDEX idx_eval_runs_status (status, id)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
    """,
    """
    CREATE TABLE IF NOT EXISTS eval_items (
        id INT AUTO_INCREMENT PRIMARY KEY,
        eval_run_id INT NOT NULL,
        run_id INT NULL,
        task_id VARCHAR(120) NOT NULL,
        category VARCHAR(80) DEFAULT '',
        passed TINYINT DEFAULT 0,
        score FLOAT DEFAULT 0,
        latency_ms INT DEFAULT 0,
        output_text MEDIUMTEXT,
        details_json MEDIUMTEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        INDEX idx_eval_items_run (eval_run_id, id),
        INDEX idx_eval_items_task (task_id)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
    """,
    """
    CREATE TABLE IF NOT EXISTS provider_settings (
        provider VARCHAR(80) PRIMARY KEY,
        api_key MEDIUMTEXT NULL,
        base_url TEXT NULL,
        models_json MEDIUMTEXT NULL,
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
    """,
    """
    CREATE TABLE IF NOT EXISTS app_settings (
        setting_key VARCHAR(120) PRIMARY KEY,
        setting_value MEDIUMTEXT NOT NULL,
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
    """,
    """
    CREATE TABLE IF NOT EXISTS project_brief (
        id INT AUTO_INCREMENT PRIMARY KEY,
        workspace VARCHAR(500) NOT NULL,
        goal MEDIUMTEXT,
        stack MEDIUMTEXT,
        conventions MEDIUMTEXT,
        updated_by INT NULL,
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
        UNIQUE KEY uq_project_brief_ws (workspace)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
    """,
    """
    CREATE TABLE IF NOT EXISTS project_tasks (
        id INT AUTO_INCREMENT PRIMARY KEY,
        workspace VARCHAR(500) NOT NULL,
        title VARCHAR(400) NOT NULL,
        owner VARCHAR(200) DEFAULT '',
        depends_on VARCHAR(400) DEFAULT '',
        deliverable MEDIUMTEXT,
        status VARCHAR(40) DEFAULT 'todo',
        notes MEDIUMTEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
        INDEX idx_project_tasks_ws (workspace, id)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
    """,
]


SQLITE_SCHEMA = [
    """
    CREATE TABLE IF NOT EXISTS agents (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        title TEXT DEFAULT '',
        status TEXT DEFAULT 'idle',
        provider TEXT DEFAULT '',
        model TEXT DEFAULT '',
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_agents_status ON agents (status, id)",
    """
    CREATE TABLE IF NOT EXISTS messages (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        agent_id INTEGER NOT NULL,
        role TEXT NOT NULL,
        content TEXT NOT NULL,
        run_id INTEGER,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_messages_agent ON messages (agent_id, id)",
    "CREATE INDEX IF NOT EXISTS idx_messages_run ON messages (run_id)",
    """
    CREATE TABLE IF NOT EXISTS shared_context (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        source_agent_id INTEGER,
        role TEXT NOT NULL,
        content TEXT NOT NULL,
        importance INTEGER DEFAULT 1,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_context_created ON shared_context (created_at)",
    "CREATE INDEX IF NOT EXISTS idx_context_source ON shared_context (source_agent_id, id)",
    """
    CREATE TABLE IF NOT EXISTS runs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        agent_id INTEGER,
        status TEXT NOT NULL,
        provider TEXT DEFAULT '',
        model TEXT DEFAULT '',
        user_prompt TEXT,
        final_output TEXT,
        error_text TEXT,
        artifact_path TEXT,
        latency_ms INTEGER DEFAULT 0,
        tool_count INTEGER DEFAULT 0,
        loop_count INTEGER DEFAULT 0,
        token_estimate INTEGER DEFAULT 0,
        started_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        ended_at TIMESTAMP
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_runs_agent ON runs (agent_id, id)",
    "CREATE INDEX IF NOT EXISTS idx_runs_status ON runs (status, id)",
    """
    CREATE TABLE IF NOT EXISTS run_events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        run_id INTEGER NOT NULL,
        event_type TEXT NOT NULL,
        payload_json TEXT NOT NULL,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_events_run ON run_events (run_id, id)",
    "CREATE INDEX IF NOT EXISTS idx_events_type ON run_events (event_type, id)",
    """
    CREATE TABLE IF NOT EXISTS plans (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        run_id INTEGER NOT NULL,
        step_text TEXT NOT NULL,
        status TEXT DEFAULT 'pending',
        sort_order INTEGER DEFAULT 0,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_plans_run ON plans (run_id, sort_order)",
    """
    CREATE TABLE IF NOT EXISTS eval_runs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        status TEXT NOT NULL,
        provider TEXT DEFAULT '',
        model TEXT DEFAULT '',
        score REAL DEFAULT 0,
        passed INTEGER DEFAULT 0,
        total INTEGER DEFAULT 0,
        latency_ms INTEGER DEFAULT 0,
        summary_json TEXT,
        error_text TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        ended_at TIMESTAMP
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_eval_runs_status ON eval_runs (status, id)",
    """
    CREATE TABLE IF NOT EXISTS eval_items (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        eval_run_id INTEGER NOT NULL,
        run_id INTEGER,
        task_id TEXT NOT NULL,
        category TEXT DEFAULT '',
        passed INTEGER DEFAULT 0,
        score REAL DEFAULT 0,
        latency_ms INTEGER DEFAULT 0,
        output_text TEXT,
        details_json TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_eval_items_run ON eval_items (eval_run_id, id)",
    "CREATE INDEX IF NOT EXISTS idx_eval_items_task ON eval_items (task_id)",
    """
    CREATE TABLE IF NOT EXISTS provider_settings (
        provider TEXT PRIMARY KEY,
        api_key TEXT,
        base_url TEXT,
        models_json TEXT,
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS app_settings (
        setting_key TEXT PRIMARY KEY,
        setting_value TEXT NOT NULL,
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS project_brief (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        workspace TEXT NOT NULL UNIQUE,
        goal TEXT,
        stack TEXT,
        conventions TEXT,
        updated_by INTEGER,
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS project_tasks (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        workspace TEXT NOT NULL,
        title TEXT NOT NULL,
        owner TEXT DEFAULT '',
        depends_on TEXT DEFAULT '',
        deliverable TEXT,
        status TEXT DEFAULT 'todo',
        notes TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
    """,
]
