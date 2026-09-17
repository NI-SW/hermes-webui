from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from db import ensure_chat_schema, sqlite_connection


def empty_knowledge_configuration() -> dict[str, Any]:
    return {
        "configured": False,
        "vector_search_host": "",
        "rag_service_mcp_url": "",
        "updated_at": None,
    }


def get_knowledge_configuration() -> dict[str, Any] | None:
    ensure_chat_schema()
    with sqlite_connection() as connection:
        row = connection.execute(
            """
            SELECT vector_search_host, rag_service_mcp_url, updated_at
            FROM knowledge_service_config
            WHERE id = 1
            """
        ).fetchone()
    if row is None:
        return None
    return {
        "configured": True,
        "vector_search_host": row["vector_search_host"],
        "rag_service_mcp_url": row["rag_service_mcp_url"],
        "updated_at": row["updated_at"],
    }


def save_knowledge_configuration(
    vector_search_host: str,
    rag_service_mcp_url: str,
) -> dict[str, Any]:
    ensure_chat_schema()
    updated_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with sqlite_connection() as connection:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            """
            INSERT INTO knowledge_service_config (
                id,
                vector_search_host,
                rag_service_mcp_url,
                updated_at
            ) VALUES (1, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                vector_search_host = excluded.vector_search_host,
                rag_service_mcp_url = excluded.rag_service_mcp_url,
                updated_at = excluded.updated_at
            """,
            (vector_search_host, rag_service_mcp_url, updated_at),
        )
        connection.commit()
    return {
        "configured": True,
        "vector_search_host": vector_search_host,
        "rag_service_mcp_url": rag_service_mcp_url,
        "updated_at": updated_at,
    }
