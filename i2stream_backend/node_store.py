from __future__ import annotations

from datetime import datetime, timezone
from ipaddress import ip_address
from typing import TypedDict

from db import ensure_chat_schema, sqlite_connection

OFFLINE_AFTER_SECONDS = 90


class NodeStatus(TypedDict):
    ip: str
    online: bool
    last_seen_at: str


def normalize_node_ip(value: str) -> str:
    if "%" in value:
        raise ValueError("scoped IPv6 addresses are not supported")
    return str(ip_address(value))


def _utc_timestamp(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamp must include a timezone")
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _parse_utc_timestamp(value: str) -> datetime:
    timestamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if timestamp.tzinfo is None or timestamp.utcoffset() is None:
        raise ValueError("stored timestamp must include a timezone")
    return timestamp.astimezone(timezone.utc)


def record_heartbeat(ip: str, received_at: datetime) -> None:
    canonical_ip = normalize_node_ip(ip)
    received_timestamp = _utc_timestamp(received_at)
    ensure_chat_schema()
    with sqlite_connection() as connection:
        connection.execute(
            """
            INSERT INTO node_heartbeats (ip, last_seen_at)
            VALUES (?, ?)
            ON CONFLICT(ip) DO UPDATE SET last_seen_at = excluded.last_seen_at
            """,
            (canonical_ip, received_timestamp),
        )
        connection.commit()


def list_nodes(observed_at: datetime) -> list[NodeStatus]:
    observed_timestamp = _parse_utc_timestamp(_utc_timestamp(observed_at))
    ensure_chat_schema()
    with sqlite_connection() as connection:
        rows = connection.execute(
            """
            SELECT ip, last_seen_at
            FROM node_heartbeats
            ORDER BY ip
            """
        ).fetchall()

    nodes: list[NodeStatus] = []
    for row in rows:
        last_seen_at = _parse_utc_timestamp(row["last_seen_at"])
        age_seconds = (observed_timestamp - last_seen_at).total_seconds()
        nodes.append(
            {
                "ip": row["ip"],
                "online": age_seconds <= OFFLINE_AFTER_SECONDS,
                "last_seen_at": _utc_timestamp(last_seen_at),
            }
        )
    return nodes
