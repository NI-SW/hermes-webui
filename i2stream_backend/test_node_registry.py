from __future__ import annotations

import os
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import httpx
from pydantic import ValidationError

os.environ.setdefault("SESSION_HMAC_SECRET", "session-secret-32-bytes-for-tests!!")
os.environ.setdefault("GATEWAY_BRIDGE_TOKEN", "gateway-token-32-bytes-for-tests!!!")

import db
import main
from models import HeartbeatRequest
from node_store import OFFLINE_AFTER_SECONDS, delete_node, list_nodes, record_heartbeat


class HeartbeatRequestTests(unittest.TestCase):
    def test_canonicalizes_ipv4_and_ipv6(self) -> None:
        self.assertEqual(HeartbeatRequest(ip="10.1.1.10").ip, "10.1.1.10")
        self.assertEqual(
            HeartbeatRequest(ip="2001:0DB8:0000:0000:0000:0000:0000:0001").ip,
            "2001:db8::1",
        )

    def test_rejects_invalid_ip_non_string_and_extra_fields(self) -> None:
        invalid_payloads = (
            {"ip": "node-a"},
            {"ip": " 10.1.1.10 "},
            {"ip": 167837962},
            {"ip": "10.1.1.10", "name": "node-a"},
        )
        for payload in invalid_payloads:
            with self.subTest(payload=payload), self.assertRaises(ValidationError):
                HeartbeatRequest.model_validate(payload)


class NodeStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        db.chat_schema_ready = False

    def tearDown(self) -> None:
        db.chat_schema_ready = False

    def test_heartbeat_upserts_and_nodes_persist_across_store_calls(self) -> None:
        first_seen = datetime(2026, 8, 31, 1, 2, 3, tzinfo=timezone.utc)
        last_seen = first_seen + timedelta(seconds=60)

        with tempfile.TemporaryDirectory() as temp_dir:
            sqlite_path = Path(temp_dir) / "chat.db"
            with patch.object(db, "SQLITE_DB_PATH", sqlite_path):
                record_heartbeat("10.1.1.10", first_seen)
                record_heartbeat("10.1.1.10", last_seen)

                nodes = list_nodes(last_seen + timedelta(seconds=1))

        self.assertEqual(
            nodes,
            [
                {
                    "ip": "10.1.1.10",
                    "online": True,
                    "first_seen_at": "2026-08-31T01:02:03Z",
                    "last_seen_at": "2026-08-31T01:03:03Z",
                }
            ],
        )

    def test_migrates_existing_nodes_with_last_seen_as_earliest_known_time(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            sqlite_path = Path(temp_dir) / "chat.db"
            with sqlite3.connect(sqlite_path) as connection:
                connection.execute(
                    "CREATE TABLE node_heartbeats (ip TEXT PRIMARY KEY, last_seen_at TEXT NOT NULL)"
                )
                connection.execute(
                    "INSERT INTO node_heartbeats (ip, last_seen_at) VALUES (?, ?)",
                    ("10.1.1.9", "2026-08-31T01:02:03Z"),
                )

            with patch.object(db, "SQLITE_DB_PATH", sqlite_path):
                nodes = list_nodes(
                    datetime(2026, 8, 31, 1, 4, 0, tzinfo=timezone.utc)
                )
                with db.sqlite_connection() as connection:
                    columns = {
                        row["name"]: row["notnull"]
                        for row in connection.execute(
                            "PRAGMA table_info(node_heartbeats)"
                        ).fetchall()
                    }

        self.assertEqual(columns["first_seen_at"], 1)
        self.assertEqual(nodes[0]["first_seen_at"], "2026-08-31T01:02:03Z")
        self.assertEqual(nodes[0]["last_seen_at"], "2026-08-31T01:02:03Z")

    def test_failed_migration_rolls_back_without_renaming_or_losing_nodes(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            sqlite_path = Path(temp_dir) / "chat.db"
            with sqlite3.connect(sqlite_path) as connection:
                connection.execute(
                    "CREATE TABLE node_heartbeats (ip TEXT PRIMARY KEY, last_seen_at TEXT NOT NULL)"
                )
                connection.execute(
                    "INSERT INTO node_heartbeats (ip, last_seen_at) VALUES (?, ?)",
                    ("10.1.1.9", "2026-08-31T01:02:03Z"),
                )

            with (
                patch.object(db, "SQLITE_DB_PATH", sqlite_path),
                patch.object(db, "NODE_HEARTBEAT_SCHEMA_STATEMENT", "INVALID SQL"),
                self.assertRaises(sqlite3.OperationalError),
            ):
                db.ensure_chat_schema()

            with sqlite3.connect(sqlite_path) as connection:
                tables = {
                    row[0]
                    for row in connection.execute(
                        "SELECT name FROM sqlite_master WHERE type = 'table'"
                    ).fetchall()
                }
                rows = connection.execute(
                    "SELECT ip, last_seen_at FROM node_heartbeats"
                ).fetchall()

        self.assertIn("node_heartbeats", tables)
        self.assertNotIn("node_heartbeats_before_first_seen", tables)
        self.assertEqual(rows, [("10.1.1.9", "2026-08-31T01:02:03Z")])

    def test_delete_removes_node_until_it_reports_another_heartbeat(self) -> None:
        first_seen = datetime(2026, 8, 31, 1, 2, 3, tzinfo=timezone.utc)
        returned_at = first_seen + timedelta(minutes=5)

        with tempfile.TemporaryDirectory() as temp_dir:
            sqlite_path = Path(temp_dir) / "chat.db"
            with patch.object(db, "SQLITE_DB_PATH", sqlite_path):
                record_heartbeat("10.1.1.10", first_seen)
                self.assertTrue(delete_node("10.1.1.10"))
                self.assertFalse(delete_node("10.1.1.10"))
                self.assertEqual(list_nodes(returned_at), [])

                record_heartbeat("10.1.1.10", returned_at)
                nodes = list_nodes(returned_at)

        self.assertEqual(nodes[0]["first_seen_at"], "2026-08-31T01:07:03Z")

    def test_marks_node_offline_only_after_90_seconds_and_keeps_it(self) -> None:
        last_seen = datetime(2026, 8, 31, 1, 2, 3, tzinfo=timezone.utc)

        with tempfile.TemporaryDirectory() as temp_dir:
            sqlite_path = Path(temp_dir) / "chat.db"
            with patch.object(db, "SQLITE_DB_PATH", sqlite_path):
                record_heartbeat("10.1.1.10", last_seen)
                at_boundary = list_nodes(
                    last_seen + timedelta(seconds=OFFLINE_AFTER_SECONDS)
                )
                after_boundary = list_nodes(
                    last_seen + timedelta(seconds=OFFLINE_AFTER_SECONDS, microseconds=1)
                )

        self.assertTrue(at_boundary[0]["online"])
        self.assertFalse(after_boundary[0]["online"])
        self.assertEqual(after_boundary[0]["ip"], "10.1.1.10")
        self.assertEqual(after_boundary[0]["last_seen_at"], "2026-08-31T01:02:03Z")


class NodeApiTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        db.chat_schema_ready = False

    def tearDown(self) -> None:
        db.chat_schema_ready = False

    async def test_heartbeat_and_node_list_http_contract(self) -> None:
        received_at = datetime(2026, 8, 31, 1, 2, 3, tzinfo=timezone.utc)

        with tempfile.TemporaryDirectory() as temp_dir:
            sqlite_path = Path(temp_dir) / "chat.db"
            with (
                patch.object(db, "SQLITE_DB_PATH", sqlite_path),
                patch.object(main, "utc_now", return_value=received_at),
            ):
                transport = httpx.ASGITransport(app=main.app)
                async with httpx.AsyncClient(
                    transport=transport,
                    base_url="http://testserver",
                ) as client:
                    heartbeat_response = await client.post(
                        "/api/heartbeat",
                        json={"ip": "2001:0db8:0:0:0:0:0:1"},
                    )
                    nodes_response = await client.get("/api/nodes")

        self.assertEqual(heartbeat_response.status_code, 204)
        self.assertEqual(heartbeat_response.content, b"")
        self.assertEqual(nodes_response.status_code, 200)
        self.assertEqual(
            nodes_response.json(),
            {
                "offline_after_seconds": 90,
                "nodes": [
                    {
                        "ip": "2001:db8::1",
                        "online": True,
                        "first_seen_at": "2026-08-31T01:02:03Z",
                        "last_seen_at": "2026-08-31T01:02:03Z",
                    }
                ],
            },
        )

    async def test_delete_node_http_contract_is_idempotent(self) -> None:
        received_at = datetime(2026, 8, 31, 1, 2, 3, tzinfo=timezone.utc)

        with tempfile.TemporaryDirectory() as temp_dir:
            sqlite_path = Path(temp_dir) / "chat.db"
            with (
                patch.object(db, "SQLITE_DB_PATH", sqlite_path),
                patch.object(main, "utc_now", return_value=received_at),
            ):
                transport = httpx.ASGITransport(app=main.app)
                async with httpx.AsyncClient(
                    transport=transport,
                    base_url="http://testserver",
                ) as client:
                    await client.post("/api/heartbeat", json={"ip": "2001:db8::1"})
                    deleted = await client.delete("/api/nodes/2001%3Adb8%3A%3A1")
                    missing = await client.delete("/api/nodes/2001%3Adb8%3A%3A1")
                    nodes = await client.get("/api/nodes")

        self.assertEqual(deleted.status_code, 204)
        self.assertEqual(deleted.content, b"")
        self.assertEqual(missing.status_code, 204)
        self.assertEqual(missing.content, b"")
        self.assertEqual(nodes.json()["nodes"], [])

    async def test_heartbeat_http_rejects_extra_fields(self) -> None:
        transport = httpx.ASGITransport(app=main.app)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://testserver",
        ) as client:
            response = await client.post(
                "/api/heartbeat",
                json={"ip": "10.1.1.10", "name": "node-a"},
            )

        self.assertEqual(response.status_code, 422)


if __name__ == "__main__":
    unittest.main()
