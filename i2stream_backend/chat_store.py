from __future__ import annotations

import json
import secrets
import sqlite3
import time
from threading import Lock
from typing import Any

from fastapi import HTTPException

from db import ensure_chat_schema, sqlite_connection


message_id_lock = Lock()
last_message_id = 0


def create_message_id() -> int:
    global last_message_id
    now = time.time_ns() // 1_000
    with message_id_lock:
        if now <= last_message_id:
            now = last_message_id + 1
        last_message_id = now
        return now


def issue_chat_client_id() -> str:
    ensure_chat_schema()
    with sqlite_connection() as connection:
        for _ in range(10):
            client_id = secrets.token_hex(32)
            try:
                connection.execute(
                    "INSERT INTO chat_clients (client_id) VALUES (?)",
                    (client_id,),
                )
            except sqlite3.IntegrityError:
                continue
            else:
                connection.commit()
                return client_id
        raise HTTPException(status_code=500, detail="Unable to allocate unique client id")


def insert_chat_message(
    client_id: str,
    conversation_id: str,
    role: str,
    content: str = "",
    payload: dict[str, Any] | None = None,
) -> int:
    ensure_chat_schema()
    with sqlite_connection() as connection:
        for _ in range(10):
            message_id = create_message_id()
            try:
                connection.execute(
                    """
                    INSERT INTO chat_messages
                        (id, client_id, conversation_id, role, content, payload_json)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        message_id,
                        client_id,
                        conversation_id,
                        role,
                        content,
                        json.dumps(payload, ensure_ascii=False) if payload is not None else None,
                    ),
                )
            except sqlite3.IntegrityError:
                continue
            else:
                connection.commit()
                return message_id
        raise HTTPException(status_code=500, detail="Unable to allocate unique message id")


def list_chat_messages(client_id: str, conversation_id: str) -> list[dict[str, Any]]:
    ensure_chat_schema()
    with sqlite_connection() as connection:
        state_row = connection.execute(
            """
            SELECT COALESCE(hidden_before_message_id, 0) AS hidden_before_message_id
            FROM chat_state
            WHERE client_id = ? AND conversation_id = ?
            """,
            (client_id, conversation_id),
        ).fetchone()
        hidden_before = int(state_row["hidden_before_message_id"]) if state_row else 0
        rows = connection.execute(
            """
            SELECT message.id, message.role, message.content, message.payload_json,
                   message.created_at,
                   feedback.feedback AS interaction_feedback,
                   feedback.status AS interaction_status,
                   feedback.job_id AS interaction_job_id,
                   feedback.datacop_problem_id AS interaction_datacop_problem_id,
                   feedback.error AS interaction_error
            FROM chat_messages AS message
            LEFT JOIN message_feedback AS feedback
              ON feedback.client_id = message.client_id
             AND feedback.conversation_id = message.conversation_id
             AND feedback.message_id = message.id
            WHERE message.client_id = ?
              AND message.conversation_id = ?
              AND message.id > ?
            ORDER BY message.id ASC
            LIMIT 500
            """,
            (client_id, conversation_id, hidden_before),
        ).fetchall()

    messages: list[dict[str, Any]] = []
    for row in rows:
        messages.append(chat_message_from_row(row))
    return messages


def list_chat_messages_through(
    client_id: str,
    conversation_id: str,
    message_id: int,
) -> list[dict[str, Any]]:
    ensure_chat_schema()
    with sqlite_connection() as connection:
        state_row = connection.execute(
            """
            SELECT COALESCE(hidden_before_message_id, 0) AS hidden_before_message_id
            FROM chat_state
            WHERE client_id = ? AND conversation_id = ?
            """,
            (client_id, conversation_id),
        ).fetchone()
        hidden_before = int(state_row["hidden_before_message_id"]) if state_row else 0
        rows = connection.execute(
            """
            SELECT id, role, content, payload_json, created_at
            FROM chat_messages
            WHERE client_id = ?
              AND conversation_id = ?
              AND id > ?
              AND id <= ?
            ORDER BY id ASC
            """,
            (client_id, conversation_id, hidden_before, message_id),
        ).fetchall()
    return [chat_message_from_row(row) for row in rows]


def assistant_message_exists(client_id: str, conversation_id: str, message_id: int) -> bool:
    ensure_chat_schema()
    with sqlite_connection() as connection:
        row = connection.execute(
            """
            SELECT 1
            FROM chat_messages AS message
            LEFT JOIN chat_state AS state
              ON state.client_id = message.client_id
             AND state.conversation_id = message.conversation_id
            WHERE message.id = ?
              AND message.client_id = ?
              AND message.conversation_id = ?
              AND message.role = 'assistant'
              AND message.id > COALESCE(state.hidden_before_message_id, 0)
            """,
            (message_id, client_id, conversation_id),
        ).fetchone()
    return row is not None


def list_client_chat_history(client_id: str) -> list[dict[str, Any]]:
    ensure_chat_schema()
    with sqlite_connection() as connection:
        conversation_rows = connection.execute(
            """
            SELECT conversation_id, MAX(id) AS latest_message_id, COUNT(*) AS message_count
            FROM chat_messages
            WHERE client_id = ?
            GROUP BY conversation_id
            ORDER BY latest_message_id DESC
            """,
            (client_id,),
        ).fetchall()

        conversations: list[dict[str, Any]] = []
        for conversation_row in conversation_rows:
            conversation_id = conversation_row["conversation_id"]
            rows = connection.execute(
                """
                SELECT message.id, message.role, message.content, message.payload_json,
                       message.created_at,
                       feedback.feedback AS interaction_feedback,
                       feedback.status AS interaction_status,
                       feedback.job_id AS interaction_job_id,
                       feedback.datacop_problem_id AS interaction_datacop_problem_id,
                       feedback.error AS interaction_error
                FROM chat_messages AS message
                LEFT JOIN message_feedback AS feedback
                  ON feedback.client_id = message.client_id
                 AND feedback.conversation_id = message.conversation_id
                 AND feedback.message_id = message.id
                WHERE message.client_id = ?
                  AND message.conversation_id = ?
                ORDER BY message.id ASC
                LIMIT 500
                """,
                (client_id, conversation_id),
            ).fetchall()
            conversations.append(
                {
                    "conversation_id": conversation_id,
                    "message_count": int(conversation_row["message_count"]),
                    "latest_message_id": int(conversation_row["latest_message_id"]),
                    "messages": [chat_message_from_row(row) for row in rows],
                }
            )
    return conversations


def list_client_chat_conversation_summaries(
    client_id: str,
    limit: int,
    before: int | None,
) -> tuple[list[dict[str, Any]], int | None]:
    if not 1 <= limit <= 100:
        raise ValueError("limit must be between 1 and 100")
    if before is not None and before <= 0:
        raise ValueError("before must be a positive message id")

    ensure_chat_schema()
    cursor_clause = ""
    parameters: list[Any] = [client_id]
    if before is not None:
        cursor_clause = "WHERE summary.latest_message_id < ?"
        parameters.append(before)
    parameters.append(limit + 1)

    with sqlite_connection() as connection:
        rows = connection.execute(
            f"""
            WITH visible_messages AS (
                SELECT message.id, message.client_id, message.conversation_id,
                       message.content, message.created_at
                FROM chat_messages AS message
                LEFT JOIN chat_state AS state
                  ON state.client_id = message.client_id
                 AND state.conversation_id = message.conversation_id
                WHERE message.client_id = ?
                  AND message.id > COALESCE(state.hidden_before_message_id, 0)
            ),
            summary AS (
                SELECT conversation_id, COUNT(*) AS message_count,
                       MAX(id) AS latest_message_id
                FROM visible_messages
                GROUP BY conversation_id
            )
            SELECT summary.conversation_id, summary.message_count,
                   summary.latest_message_id, latest.content,
                   latest.created_at AS latest_message_at
            FROM summary
            JOIN visible_messages AS latest
              ON latest.conversation_id = summary.conversation_id
             AND latest.id = summary.latest_message_id
            {cursor_clause}
            ORDER BY summary.latest_message_id DESC
            LIMIT ?
            """,
            parameters,
        ).fetchall()

    has_more = len(rows) > limit
    page_rows = rows[:limit]
    conversations = [
        {
            "client_id": client_id,
            "conversation_id": row["conversation_id"],
            "message_count": int(row["message_count"]),
            "latest_message_id": int(row["latest_message_id"]),
            "latest_message_at": row["latest_message_at"],
            "preview": (row["content"] or "")[:200],
        }
        for row in page_rows
    ]
    next_before = conversations[-1]["latest_message_id"] if has_more else None
    return conversations, next_before


def chat_message_from_row(row: Any) -> dict[str, Any]:
    payload = row["payload_json"]
    created_at = row["created_at"]
    interaction = None
    if "interaction_feedback" in row.keys() and row["interaction_feedback"] is not None:
        interaction = {
            "feedback": row["interaction_feedback"],
            "status": row["interaction_status"],
            "job_id": row["interaction_job_id"],
            "datacop_problem_id": row["interaction_datacop_problem_id"],
            "error": row["interaction_error"],
        }
    return {
        "id": row["id"],
        "role": row["role"],
        "content": row["content"] or "",
        "payload": json.loads(payload) if isinstance(payload, str) and payload else None,
        "created_at": created_at.isoformat() if hasattr(created_at, "isoformat") else created_at,
        "dialog_interaction": interaction,
    }


def clear_visible_chat_messages(client_id: str, conversation_id: str) -> int:
    ensure_chat_schema()
    with sqlite_connection() as connection:
        row = connection.execute(
            """
            SELECT COALESCE(MAX(id), 0) AS max_id
            FROM chat_messages
            WHERE client_id = ? AND conversation_id = ?
            """,
            (client_id, conversation_id),
        ).fetchone()
        max_id = int(row["max_id"]) if row else 0
        connection.execute(
            """
            INSERT INTO chat_state
                (client_id, conversation_id, hidden_before_message_id)
            VALUES (?, ?, ?)
            ON CONFLICT(client_id, conversation_id) DO UPDATE SET
                hidden_before_message_id = excluded.hidden_before_message_id,
                updated_at = CURRENT_TIMESTAMP
            """,
            (client_id, conversation_id, max_id),
        )
        connection.commit()
    return max_id
