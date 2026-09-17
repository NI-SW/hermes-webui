from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("SESSION_HMAC_SECRET", "session-secret-32-bytes-for-tests!!")
os.environ.setdefault("GATEWAY_BRIDGE_TOKEN", "gateway-token-32-bytes-for-tests!!!")

import chat_store
import db
import main


class ChatHistoryLookupTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        db.chat_schema_ready = False

    def tearDown(self) -> None:
        db.chat_schema_ready = False

    async def test_chat_history_groups_messages_by_conversation_for_client(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            sqlite_path = Path(temp_dir) / "chat.db"
            with patch.object(db, "SQLITE_DB_PATH", sqlite_path):
                client_id = chat_store.issue_chat_client_id()
                other_client_id = chat_store.issue_chat_client_id()
                chat_store.insert_chat_message(client_id, "conversation-a", "user", "hello")
                chat_store.insert_chat_message(client_id, "conversation-a", "assistant", "world")
                chat_store.insert_chat_message(client_id, "conversation-b", "user", "again")
                chat_store.insert_chat_message(other_client_id, "conversation-a", "user", "other")

                payload = await main.chat_history(client_id)

        self.assertEqual(payload["code"], 0)
        self.assertEqual(payload["client_id"], client_id)
        self.assertEqual([item["conversation_id"] for item in payload["conversations"]], ["conversation-b", "conversation-a"])
        self.assertEqual(payload["conversations"][0]["messages"][0]["content"], "again")
        self.assertEqual([message["role"] for message in payload["conversations"][1]["messages"]], ["user", "assistant"])
        self.assertEqual(payload["conversations"][1]["messages"][0]["content"], "hello")

    async def test_client_scoped_conversation_summaries_paginate_by_latest_message(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            sqlite_path = Path(temp_dir) / "chat.db"
            with patch.object(db, "SQLITE_DB_PATH", sqlite_path):
                client_id = chat_store.issue_chat_client_id()
                other_client_id = chat_store.issue_chat_client_id()
                first_id = chat_store.insert_chat_message(client_id, "conversation-a", "user", "first")
                second_id = chat_store.insert_chat_message(client_id, "conversation-b", "user", "second")
                chat_store.insert_chat_message(other_client_id, "conversation-other", "user", "private")
                third_id = chat_store.insert_chat_message(client_id, "conversation-c", "assistant", "third")

                first_page = await main.chat_conversations(client_id, 2, None)
                second_page = await main.chat_conversations(
                    client_id,
                    2,
                    first_page["next_before"],
                )

        self.assertEqual(
            [item["conversation_id"] for item in first_page["conversations"]],
            ["conversation-c", "conversation-b"],
        )
        self.assertEqual(first_page["next_before"], second_id)
        self.assertEqual(first_page["conversations"][0]["client_id"], client_id)
        self.assertEqual(first_page["conversations"][0]["preview"], "third")
        self.assertEqual(first_page["conversations"][0]["latest_message_id"], third_id)
        self.assertEqual(
            [item["conversation_id"] for item in second_page["conversations"]],
            ["conversation-a"],
        )
        self.assertEqual(second_page["conversations"][0]["latest_message_id"], first_id)
        self.assertIsNone(second_page["next_before"])

    async def test_client_scoped_conversation_summaries_exclude_cleared_messages(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            sqlite_path = Path(temp_dir) / "chat.db"
            with patch.object(db, "SQLITE_DB_PATH", sqlite_path):
                client_id = chat_store.issue_chat_client_id()
                chat_store.insert_chat_message(client_id, "cleared", "user", "old content")
                chat_store.clear_visible_chat_messages(client_id, "cleared")
                chat_store.insert_chat_message(client_id, "visible", "user", "new content")

                payload = await main.chat_conversations(client_id, 50, None)

        self.assertEqual(
            [item["conversation_id"] for item in payload["conversations"]],
            ["visible"],
        )
        self.assertIsNone(payload["next_before"])


if __name__ == "__main__":
    unittest.main()
