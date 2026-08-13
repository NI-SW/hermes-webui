from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from sqlite3 import IntegrityError
from unittest.mock import patch

import chat_store
import db
from dashboard_session_store import SQLiteDashboardSessionStore
from dialoginteract import MessageFeedbackRecord, SQLiteFeedbackStore


class SqliteConfigTests(unittest.TestCase):
    def test_sqlite_path_is_fixed_under_app_data(self) -> None:
        self.assertEqual(db.SQLITE_DB_PATH, Path("/app/data/agent-console/chat.db"))


class SqliteStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        db.chat_schema_ready = False

    def tearDown(self) -> None:
        db.chat_schema_ready = False

    def test_chat_schema_avoids_mysql_storage_clauses(self) -> None:
        schema_sql = "\n".join(db.CHAT_SCHEMA_STATEMENTS).upper()

        self.assertNotIn("CREATE DATABASE", schema_sql)
        self.assertNotIn("ENGINE=", schema_sql)
        self.assertNotIn("AUTO_INCREMENT", schema_sql)

    def test_dashboard_session_ownership_is_persistent_and_user_scoped(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            sqlite_path = Path(temp_dir) / "chat.db"
            with patch.object(db, "SQLITE_DB_PATH", sqlite_path):
                store = SQLiteDashboardSessionStore()
                store.add("a" * 64, "api_session_a")
                store.add("a" * 64, "api_session_b")
                store.add("b" * 64, "api_session_c")

                first_user_sessions = SQLiteDashboardSessionStore().list_session_ids("a" * 64)
                second_user_owns_first = store.owns("b" * 64, "api_session_a")
                removed_by_wrong_user = store.remove("b" * 64, "api_session_a")
                removed_by_owner = store.remove("a" * 64, "api_session_a")

        self.assertEqual(first_user_sessions, ["api_session_b", "api_session_a"])
        self.assertFalse(second_user_owns_first)
        self.assertFalse(removed_by_wrong_user)
        self.assertTrue(removed_by_owner)

    def test_chat_store_round_trip_and_clear_visible_history(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            sqlite_path = Path(temp_dir) / "chat.db"

            with patch.object(db, "SQLITE_DB_PATH", sqlite_path):
                client_id = chat_store.issue_chat_client_id()
                first_id = chat_store.insert_chat_message(client_id, "conversation-1", "user", "hello")
                second_id = chat_store.insert_chat_message(
                    client_id,
                    "conversation-1",
                    "assistant",
                    "world",
                    {"files": [{"token": "abc"}]},
                )

                history = chat_store.list_chat_messages(client_id, "conversation-1")
                hidden_before = chat_store.clear_visible_chat_messages(client_id, "conversation-1")
                visible_after_clear = chat_store.list_chat_messages(client_id, "conversation-1")
                third_id = chat_store.insert_chat_message(client_id, "conversation-1", "user", "again")
                visible_after_new_message = chat_store.list_chat_messages(client_id, "conversation-1")

        self.assertLess(first_id, second_id)
        self.assertEqual([message["role"] for message in history], ["user", "assistant"])
        self.assertEqual(history[0]["content"], "hello")
        self.assertEqual(history[1]["payload"], {"files": [{"token": "abc"}]})
        self.assertEqual(hidden_before, second_id)
        self.assertEqual(visible_after_clear, [])
        self.assertGreater(third_id, hidden_before)
        self.assertEqual(len(visible_after_new_message), 1)
        self.assertEqual(visible_after_new_message[0]["content"], "again")

    def test_chat_snapshot_stops_at_liked_assistant_and_respects_clear_boundary(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            sqlite_path = Path(temp_dir) / "chat.db"

            with patch.object(db, "SQLITE_DB_PATH", sqlite_path):
                client_id = chat_store.issue_chat_client_id()
                chat_store.insert_chat_message(client_id, "conversation-1", "user", "hidden question")
                hidden_assistant_id = chat_store.insert_chat_message(
                    client_id,
                    "conversation-1",
                    "assistant",
                    "hidden answer",
                )
                chat_store.clear_visible_chat_messages(client_id, "conversation-1")
                chat_store.insert_chat_message(client_id, "conversation-1", "user", "visible question")
                liked_id = chat_store.insert_chat_message(
                    client_id,
                    "conversation-1",
                    "assistant",
                    "liked answer",
                )
                chat_store.insert_chat_message(client_id, "conversation-1", "user", "later question")

                snapshot = chat_store.list_chat_messages_through(
                    client_id,
                    "conversation-1",
                    liked_id,
                )
                hidden_exists = chat_store.assistant_message_exists(
                    client_id,
                    "conversation-1",
                    hidden_assistant_id,
                )
                liked_exists = chat_store.assistant_message_exists(
                    client_id,
                    "conversation-1",
                    liked_id,
                )

        self.assertEqual(
            [(message["role"], message["content"]) for message in snapshot],
            [("user", "visible question"), ("assistant", "liked answer")],
        )
        self.assertFalse(hidden_exists)
        self.assertTrue(liked_exists)

    def test_message_feedback_persists_and_is_returned_with_chat_history(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            sqlite_path = Path(temp_dir) / "chat.db"
            with patch.object(db, "SQLITE_DB_PATH", sqlite_path):
                client_id = chat_store.issue_chat_client_id()
                chat_store.insert_chat_message(
                    client_id,
                    "conversation-1",
                    "user",
                    "question",
                )
                message_id = chat_store.insert_chat_message(
                    client_id,
                    "conversation-1",
                    "assistant",
                    "answer",
                )
                store = SQLiteFeedbackStore()
                record = MessageFeedbackRecord(
                    client_id=client_id,
                    conversation_id="conversation-1",
                    message_id=message_id,
                    feedback="like",
                    status="queued",
                    job_id="job-1",
                    datacop_problem_id=None,
                    error=None,
                )
                created, stored = store.create(record)
                duplicate_created, duplicate = SQLiteFeedbackStore().create(record)
                updated = store.update_job(
                    "job-1",
                    status="succeeded",
                    datacop_problem_id=91,
                    error=None,
                )
                history = chat_store.list_chat_messages(client_id, "conversation-1")

        self.assertTrue(created)
        self.assertFalse(duplicate_created)
        self.assertEqual(duplicate, stored)
        self.assertEqual(updated.status, "succeeded")
        self.assertIsNone(history[0]["dialog_interaction"])
        self.assertEqual(
            history[1]["dialog_interaction"],
            {
                "feedback": "like",
                "status": "succeeded",
                "job_id": "job-1",
                "datacop_problem_id": 91,
                "error": None,
            },
        )

    def test_incomplete_persisted_feedback_is_failed_after_restart(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            sqlite_path = Path(temp_dir) / "chat.db"
            with patch.object(db, "SQLITE_DB_PATH", sqlite_path):
                client_id = chat_store.issue_chat_client_id()
                message_id = chat_store.insert_chat_message(
                    client_id,
                    "conversation-1",
                    "assistant",
                    "answer",
                )
                store = SQLiteFeedbackStore()
                store.create(
                    MessageFeedbackRecord(
                        client_id=client_id,
                        conversation_id="conversation-1",
                        message_id=message_id,
                        feedback="like",
                        status="uploading",
                        job_id="job-1",
                        datacop_problem_id=None,
                        error=None,
                    )
                )

                changed = store.fail_incomplete("Background task interrupted")
                restored = SQLiteFeedbackStore().get_by_job(client_id, "job-1")

        self.assertEqual(changed, 1)
        self.assertIsNotNone(restored)
        self.assertEqual(restored.status, "failed")
        self.assertEqual(restored.error, "Background task interrupted")

    def test_feedback_schema_rejects_incomplete_terminal_states(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            sqlite_path = Path(temp_dir) / "chat.db"
            with patch.object(db, "SQLITE_DB_PATH", sqlite_path):
                db.ensure_chat_schema()
                invalid_rows = (
                    ("like", "succeeded", "job-success", None, None),
                    ("like", "failed", "job-failure", None, "  "),
                    ("dislike", "received", None, 91, None),
                )
                for index, row in enumerate(invalid_rows, start=1):
                    with self.subTest(row=row), self.assertRaises(IntegrityError):
                        with db.sqlite_connection() as connection:
                            connection.execute(
                                """
                                INSERT INTO message_feedback (
                                    client_id, conversation_id, message_id, feedback,
                                    status, job_id, datacop_problem_id, error
                                ) VALUES (?, 'conversation-1', ?, ?, ?, ?, ?, ?)
                                """,
                                ("c" * 64, index, *row),
                            )


if __name__ == "__main__":
    unittest.main()
