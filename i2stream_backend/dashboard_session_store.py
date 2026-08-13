"""Dashboard 匿名用户与 Hermes 会话的 SQLite 归属关系。"""

from __future__ import annotations

from typing import Protocol

from db import ensure_chat_schema, sqlite_connection


class DashboardSessionStore(Protocol):
    def add(self, owner_id: str, session_id: str) -> None: ...

    def list_session_ids(self, owner_id: str) -> list[str]: ...

    def owns(self, owner_id: str, session_id: str) -> bool: ...

    def remove(self, owner_id: str, session_id: str) -> bool: ...


class SQLiteDashboardSessionStore:
    def add(self, owner_id: str, session_id: str) -> None:
        ensure_chat_schema()
        with sqlite_connection() as connection:
            connection.execute(
                """
                INSERT INTO dashboard_agent_sessions (owner_id, session_id)
                VALUES (?, ?)
                """,
                (owner_id, session_id),
            )
            connection.commit()

    def list_session_ids(self, owner_id: str) -> list[str]:
        ensure_chat_schema()
        with sqlite_connection() as connection:
            rows = connection.execute(
                """
                SELECT session_id
                FROM dashboard_agent_sessions
                WHERE owner_id = ?
                ORDER BY created_at DESC, rowid DESC
                """,
                (owner_id,),
            ).fetchall()
        return [str(row["session_id"]) for row in rows]

    def owns(self, owner_id: str, session_id: str) -> bool:
        ensure_chat_schema()
        with sqlite_connection() as connection:
            row = connection.execute(
                """
                SELECT 1
                FROM dashboard_agent_sessions
                WHERE owner_id = ? AND session_id = ?
                """,
                (owner_id, session_id),
            ).fetchone()
        return row is not None

    def remove(self, owner_id: str, session_id: str) -> bool:
        ensure_chat_schema()
        with sqlite_connection() as connection:
            cursor = connection.execute(
                """
                DELETE FROM dashboard_agent_sessions
                WHERE owner_id = ? AND session_id = ?
                """,
                (owner_id, session_id),
            )
            connection.commit()
        return cursor.rowcount == 1
