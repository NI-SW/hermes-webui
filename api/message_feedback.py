"""Persistent three-state feedback for WebUI assistant messages.

The absence of a row means no feedback.  Session transcripts remain the owner
of message content and visible context; this store contains only the stable
message reference and the selected state.
"""

import os
import sqlite3
from contextlib import closing

from api.config import STATE_DIR


_DB_FILENAME = "message_feedback.db"
_VALID_FEEDBACK = frozenset({"like", "dislike"})


def _db_path():
    return STATE_DIR / _DB_FILENAME


def _connect():
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    path = _db_path()
    created = not path.exists()
    connection = sqlite3.connect(str(path), timeout=5.0)
    connection.execute("PRAGMA busy_timeout = 5000")
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS message_feedback (
            session_id TEXT NOT NULL,
            message_ref TEXT NOT NULL,
            feedback TEXT NOT NULL CHECK (feedback IN ('like', 'dislike')),
            PRIMARY KEY (session_id, message_ref)
        )
        """
    )
    connection.commit()
    if created:
        os.chmod(path, 0o600)
    return connection


def feedback_for_session(session_id: str) -> dict[str, str]:
    with closing(_connect()) as connection:
        rows = connection.execute(
            "SELECT message_ref, feedback FROM message_feedback WHERE session_id = ?",
            (session_id,),
        ).fetchall()
    return {str(message_ref): str(feedback) for message_ref, feedback in rows}


def set_feedback(session_id: str, message_ref: str, feedback: str) -> None:
    if feedback not in _VALID_FEEDBACK:
        raise ValueError("feedback must be like or dislike")
    with closing(_connect()) as connection, connection:
        connection.execute(
            """
            INSERT INTO message_feedback (session_id, message_ref, feedback)
            VALUES (?, ?, ?)
            ON CONFLICT(session_id, message_ref)
            DO UPDATE SET feedback = excluded.feedback
            """,
            (session_id, message_ref, feedback),
        )


def clear_feedback(session_id: str, message_ref: str) -> None:
    with closing(_connect()) as connection, connection:
        connection.execute(
            "DELETE FROM message_feedback WHERE session_id = ? AND message_ref = ?",
            (session_id, message_ref),
        )


def prune_feedback_for_session(session_id: str, message_refs: set[str]) -> None:
    with closing(_connect()) as connection, connection:
        stored_refs = connection.execute(
            "SELECT message_ref FROM message_feedback WHERE session_id = ?",
            (session_id,),
        ).fetchall()
        stale_refs = [
            (session_id, message_ref)
            for (message_ref,) in stored_refs
            if message_ref not in message_refs
        ]
        connection.executemany(
            "DELETE FROM message_feedback WHERE session_id = ? AND message_ref = ?",
            stale_refs,
        )
