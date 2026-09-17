from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from threading import Lock
from typing import Any

chat_schema_lock = Lock()
chat_schema_ready = False
SQLITE_DB_PATH = Path("/app/data/agent-console/chat.db")
SQLITE_TIMEOUT_SECONDS = 5.0

NODE_HEARTBEAT_SCHEMA_STATEMENT = """
CREATE TABLE IF NOT EXISTS node_heartbeats (
    ip TEXT NOT NULL PRIMARY KEY,
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    CHECK (LENGTH(TRIM(ip)) > 0),
    CHECK (LENGTH(TRIM(first_seen_at)) > 0),
    CHECK (LENGTH(TRIM(last_seen_at)) > 0)
)
"""

CHAT_SCHEMA_STATEMENTS = (
    """
    CREATE TABLE IF NOT EXISTS chat_clients (
        client_id TEXT NOT NULL PRIMARY KEY,
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS chat_messages (
        id INTEGER NOT NULL PRIMARY KEY,
        client_id TEXT NOT NULL,
        conversation_id TEXT NOT NULL,
        role TEXT NOT NULL,
        content TEXT NULL,
        payload_json TEXT NULL,
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS chat_state (
        client_id TEXT NOT NULL,
        conversation_id TEXT NOT NULL,
        hidden_before_message_id INTEGER NOT NULL DEFAULT 0,
        updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        PRIMARY KEY (client_id, conversation_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS message_feedback (
        client_id TEXT NOT NULL,
        conversation_id TEXT NOT NULL,
        message_id INTEGER NOT NULL,
        feedback TEXT NOT NULL,
        status TEXT NOT NULL,
        job_id TEXT NULL,
        datacop_problem_id INTEGER NULL,
        error TEXT NULL,
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        PRIMARY KEY (client_id, conversation_id, message_id),
        CHECK (
            (
                feedback = 'dislike'
                AND status = 'received'
                AND job_id IS NULL
                AND datacop_problem_id IS NULL
                AND error IS NULL
            )
            OR
            (
                feedback = 'like'
                AND job_id IS NOT NULL
                AND status IN ('queued', 'summarizing', 'uploading', 'succeeded', 'failed')
            )
        ),
        CHECK (
            (status = 'succeeded' AND datacop_problem_id IS NOT NULL AND datacop_problem_id > 0)
            OR
            (status != 'succeeded' AND datacop_problem_id IS NULL)
        ),
        CHECK (
            (status = 'failed' AND error IS NOT NULL AND LENGTH(TRIM(error)) > 0)
            OR
            (status != 'failed' AND error IS NULL)
        )
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS webui_message_feedback (
        source_instance_id TEXT NOT NULL,
        session_id TEXT NOT NULL,
        message_ref TEXT NOT NULL,
        feedback TEXT NOT NULL,
        status TEXT NOT NULL,
        job_id TEXT NULL,
        datacop_problem_id INTEGER NULL,
        error TEXT NULL,
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        PRIMARY KEY (source_instance_id, session_id, message_ref),
        CHECK (
            LENGTH(source_instance_id) = 64
            AND source_instance_id NOT GLOB '*[^0-9a-f]*'
        ),
        CHECK (LENGTH(TRIM(session_id)) > 0),
        CHECK (
            LENGTH(message_ref) = 64
            AND message_ref NOT GLOB '*[^0-9a-f]*'
        ),
        CHECK (
            (
                feedback = 'dislike'
                AND status = 'received'
                AND job_id IS NULL
                AND datacop_problem_id IS NULL
                AND error IS NULL
            )
            OR
            (
                feedback = 'like'
                AND job_id IS NOT NULL
                AND status IN ('queued', 'summarizing', 'uploading', 'succeeded', 'failed')
            )
        ),
        CHECK (
            (status = 'succeeded' AND datacop_problem_id IS NOT NULL AND datacop_problem_id > 0)
            OR
            (status != 'succeeded' AND datacop_problem_id IS NULL)
        ),
        CHECK (
            (status = 'failed' AND error IS NOT NULL AND LENGTH(TRIM(error)) > 0)
            OR
            (status != 'failed' AND error IS NULL)
        )
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS dashboard_agent_sessions (
        session_id TEXT NOT NULL PRIMARY KEY,
        owner_id TEXT NOT NULL,
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        CHECK (LENGTH(owner_id) = 64),
        CHECK (LENGTH(TRIM(session_id)) > 0)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS knowledge_service_config (
        id INTEGER NOT NULL PRIMARY KEY CHECK (id = 1),
        vector_search_host TEXT NOT NULL,
        rag_service_mcp_url TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        CHECK (LENGTH(TRIM(vector_search_host)) > 0),
        CHECK (LENGTH(TRIM(rag_service_mcp_url)) > 0)
    )
    """,
    NODE_HEARTBEAT_SCHEMA_STATEMENT,
)

CHAT_SCHEMA_INDEX_STATEMENTS = (
    """
    CREATE INDEX IF NOT EXISTS idx_chat_visible
    ON chat_messages (client_id, conversation_id, id)
    """,
    """
    CREATE UNIQUE INDEX IF NOT EXISTS idx_message_feedback_job
    ON message_feedback (job_id)
    WHERE job_id IS NOT NULL
    """,
    """
    CREATE UNIQUE INDEX IF NOT EXISTS idx_webui_message_feedback_job
    ON webui_message_feedback (job_id)
    WHERE job_id IS NOT NULL
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_dashboard_agent_sessions_owner
    ON dashboard_agent_sessions (owner_id, created_at DESC)
    """,
)


@contextmanager
def sqlite_connection() -> Any:
    SQLITE_DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(str(SQLITE_DB_PATH), timeout=SQLITE_TIMEOUT_SECONDS)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA foreign_keys=ON")
        yield connection
    finally:
        connection.close()


def _migrate_node_heartbeat_schema(connection: sqlite3.Connection) -> None:
    columns = {
        row["name"]
        for row in connection.execute("PRAGMA table_info(node_heartbeats)").fetchall()
    }
    if "first_seen_at" in columns:
        return

    legacy_table = "node_heartbeats_before_first_seen"
    connection.execute(f"ALTER TABLE node_heartbeats RENAME TO {legacy_table}")
    connection.execute(NODE_HEARTBEAT_SCHEMA_STATEMENT)
    connection.execute(
        f"""
        INSERT INTO node_heartbeats (ip, first_seen_at, last_seen_at)
        SELECT ip, last_seen_at, last_seen_at
        FROM {legacy_table}
        """
    )
    connection.execute(f"DROP TABLE {legacy_table}")


def ensure_chat_schema() -> None:
    global chat_schema_ready
    if chat_schema_ready:
        return
    with chat_schema_lock:
        if chat_schema_ready:
            return

        with sqlite_connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            for statement in CHAT_SCHEMA_STATEMENTS:
                connection.execute(statement)
            _migrate_node_heartbeat_schema(connection)
            for statement in CHAT_SCHEMA_INDEX_STATEMENTS:
                connection.execute(statement)
            connection.commit()
        chat_schema_ready = True
