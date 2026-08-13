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
    CREATE TABLE IF NOT EXISTS dashboard_agent_sessions (
        session_id TEXT NOT NULL PRIMARY KEY,
        owner_id TEXT NOT NULL,
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        CHECK (LENGTH(owner_id) = 64),
        CHECK (LENGTH(TRIM(session_id)) > 0)
    )
    """,
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


def ensure_chat_schema() -> None:
    global chat_schema_ready
    if chat_schema_ready:
        return
    with chat_schema_lock:
        if chat_schema_ready:
            return

        with sqlite_connection() as connection:
            for statement in CHAT_SCHEMA_STATEMENTS:
                connection.execute(statement)
            for statement in CHAT_SCHEMA_INDEX_STATEMENTS:
                connection.execute(statement)
            connection.commit()
        chat_schema_ready = True
