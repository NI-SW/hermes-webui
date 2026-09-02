"""Persistent projection of DataCop feedback for native WebUI messages.

The 50091 compatibility service owns the asynchronous Hermes/DataCop job. This
database keeps the last acknowledged projection beside the native WebUI session
so a session can render immediately and resume polling after a reload. Rows
created by the pre-DataCop implementation remain distinguishable as ``legacy``.
"""

from __future__ import annotations

import os
import secrets
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from threading import Lock

from api.config import STATE_DIR


_DB_FILENAME = "message_feedback.db"
_INSTANCE_ID_FILENAME = "message_feedback_instance_id"
_VALID_FEEDBACK = frozenset({"like", "dislike"})
_VALID_STATUS = frozenset(
    {"legacy", "received", "queued", "summarizing", "uploading", "succeeded", "failed"}
)
_SCHEMA_LOCK = Lock()


@dataclass(frozen=True, slots=True)
class FeedbackRecord:
    session_id: str
    message_ref: str
    feedback: str
    status: str
    job_id: str | None
    datacop_problem_id: int | None
    error: str | None


def _db_path():
    return STATE_DIR / _DB_FILENAME


def _connect():
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    path = _db_path()
    created = not path.exists()
    connection = sqlite3.connect(str(path), timeout=5.0)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA busy_timeout = 5000")
    with _SCHEMA_LOCK:
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
        columns = {
            str(row["name"])
            for row in connection.execute("PRAGMA table_info(message_feedback)").fetchall()
        }
        for name, declaration in (
            ("status", "TEXT NULL"),
            ("job_id", "TEXT NULL"),
            ("datacop_problem_id", "INTEGER NULL"),
            ("error", "TEXT NULL"),
        ):
            if name not in columns:
                connection.execute(
                    f"ALTER TABLE message_feedback ADD COLUMN {name} {declaration}"
                )
        connection.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS idx_webui_message_feedback_job
            ON message_feedback (job_id)
            WHERE job_id IS NOT NULL
            """
        )
        connection.commit()
    if created:
        os.chmod(path, 0o600)
    return connection


def feedback_source_instance_id() -> str:
    """Return the persistent identity of this WebUI state directory."""
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    path = STATE_DIR / _INSTANCE_ID_FILENAME
    try:
        value = path.read_text(encoding="ascii").strip()
    except FileNotFoundError:
        value = secrets.token_hex(32)
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        try:
            descriptor = os.open(path, flags, 0o600)
        except FileExistsError:
            value = path.read_text(encoding="ascii").strip()
        else:
            with os.fdopen(descriptor, "w", encoding="ascii") as handle:
                handle.write(value + "\n")
    if len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
        raise RuntimeError("Invalid persisted WebUI feedback instance id")
    return value


def _record_from_row(row: sqlite3.Row) -> FeedbackRecord:
    raw_status = row["status"]
    status = str(raw_status) if raw_status is not None else "legacy"
    return FeedbackRecord(
        session_id=str(row["session_id"]),
        message_ref=str(row["message_ref"]),
        feedback=str(row["feedback"]),
        status=status,
        job_id=str(row["job_id"]) if row["job_id"] is not None else None,
        datacop_problem_id=(
            int(row["datacop_problem_id"])
            if row["datacop_problem_id"] is not None
            else None
        ),
        error=str(row["error"]) if row["error"] is not None else None,
    )


def feedback_for_session(session_id: str) -> dict[str, FeedbackRecord]:
    with closing(_connect()) as connection:
        rows = connection.execute(
            """
            SELECT session_id, message_ref, feedback, status, job_id,
                   datacop_problem_id, error
            FROM message_feedback WHERE session_id = ?
            """,
            (session_id,),
        ).fetchall()
    return {str(row["message_ref"]): _record_from_row(row) for row in rows}


def feedback_for_job(job_id: str) -> FeedbackRecord | None:
    with closing(_connect()) as connection:
        row = connection.execute(
            """
            SELECT session_id, message_ref, feedback, status, job_id,
                   datacop_problem_id, error
            FROM message_feedback WHERE job_id = ?
            """,
            (job_id,),
        ).fetchone()
    return _record_from_row(row) if row is not None else None


def _validate_record(record: FeedbackRecord) -> None:
    if record.feedback not in _VALID_FEEDBACK:
        raise ValueError("feedback must be like or dislike")
    if record.status not in _VALID_STATUS:
        raise ValueError("Invalid feedback status")
    if record.status == "legacy":
        if (
            record.job_id is not None
            or record.datacop_problem_id is not None
            or record.error is not None
        ):
            raise ValueError("Legacy feedback cannot contain job state")
        return
    if record.feedback == "dislike":
        if record.status != "received" or any(
            value is not None
            for value in (record.job_id, record.datacop_problem_id, record.error)
        ):
            raise ValueError("Invalid dislike feedback state")
        return
    if record.job_id is None:
        raise ValueError("Like feedback requires a job id")
    if record.status == "succeeded":
        if record.datacop_problem_id is None or record.datacop_problem_id <= 0:
            raise ValueError("Succeeded feedback requires a DataCop problem id")
    elif record.datacop_problem_id is not None:
        raise ValueError("Only succeeded feedback may contain a DataCop problem id")
    if record.status == "failed":
        if record.error is None or not record.error.strip():
            raise ValueError("Failed feedback requires an error")
    elif record.error is not None:
        raise ValueError("Only failed feedback may contain an error")


def record_feedback(record: FeedbackRecord) -> FeedbackRecord:
    """Persist an acknowledged upstream state without changing accepted feedback."""
    _validate_record(record)
    with closing(_connect()) as connection, connection:
        connection.execute(
            """
            INSERT INTO message_feedback (
                session_id, message_ref, feedback, status, job_id,
                datacop_problem_id, error
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(session_id, message_ref) DO UPDATE SET
                feedback = excluded.feedback,
                status = excluded.status,
                job_id = excluded.job_id,
                datacop_problem_id = excluded.datacop_problem_id,
                error = excluded.error
            WHERE message_feedback.status IS NULL
               OR (
                   excluded.status IS NOT NULL
                   AND message_feedback.feedback = excluded.feedback
               )
            """,
            (
                record.session_id,
                record.message_ref,
                record.feedback,
                None if record.status == "legacy" else record.status,
                record.job_id,
                record.datacop_problem_id,
                record.error,
            ),
        )
        row = connection.execute(
            """
            SELECT session_id, message_ref, feedback, status, job_id,
                   datacop_problem_id, error
            FROM message_feedback WHERE session_id = ? AND message_ref = ?
            """,
            (record.session_id, record.message_ref),
        ).fetchone()
    if row is None:
        raise RuntimeError("Message feedback write did not produce a row")
    return _record_from_row(row)


def set_feedback(session_id: str, message_ref: str, feedback: str) -> None:
    """Write a pre-DataCop feedback row for compatibility and migration tests."""
    record_feedback(
        FeedbackRecord(
            session_id=session_id,
            message_ref=message_ref,
            feedback=feedback,
            status="legacy",
            job_id=None,
            datacop_problem_id=None,
            error=None,
        )
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
            (session_id, row["message_ref"])
            for row in stored_refs
            if row["message_ref"] not in message_refs
        ]
        connection.executemany(
            "DELETE FROM message_feedback WHERE session_id = ? AND message_ref = ?",
            stale_refs,
        )
