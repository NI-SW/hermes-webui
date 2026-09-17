from __future__ import annotations

import asyncio
import os
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

os.environ.setdefault("SESSION_HMAC_SECRET", "session-secret-32-bytes-for-tests!!")
os.environ.setdefault("GATEWAY_BRIDGE_TOKEN", "gateway-token-32-bytes-for-tests!!!")

import main
import progress
from progress import (
    append_progress,
    finish_progress,
    init_progress,
    progress_lock,
    progress_records,
)


class ProgressIsolationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        with progress_lock:
            progress_records.clear()

    def tearDown(self) -> None:
        with progress_lock:
            progress_records.clear()

    async def test_same_request_id_is_isolated_between_clients(self) -> None:
        client_a = "a" * 64
        client_b = "b" * 64
        request_id = "request-id-123456"
        init_progress(client_a, request_id)
        init_progress(client_b, request_id)
        append_progress(client_a, request_id, "A reasoning")
        append_progress(client_b, request_id, "B reasoning")
        finish_progress(client_a, request_id)

        response_a = await main.get_chat_progress(request_id, client_a, 0, None)
        response_b = await main.get_chat_progress(request_id, client_b, 0, None)

        self.assertEqual(
            [event["message"] for event in response_a["events"]],
            ["Agent 已接收请求，正在准备执行。", "A reasoning"],
        )
        self.assertEqual(
            [event["message"] for event in response_b["events"]],
            ["Agent 已接收请求，正在准备执行。", "B reasoning"],
        )
        self.assertTrue(response_a["done"])
        self.assertFalse(response_b["done"])

    async def test_append_progress_returns_the_stored_event(self) -> None:
        client_id = "a" * 64
        request_id = "request-id-123456"
        init_progress(client_id, request_id)

        event = append_progress(client_id, request_id, "继续分析")

        self.assertIsNotNone(event)
        assert event is not None
        self.assertEqual(set(event), {"seq", "message", "created_at"})
        self.assertEqual(event["seq"], 2)
        self.assertEqual(event["message"], "继续分析")
        self.assertIsInstance(event["created_at"], float)

    async def test_other_client_cannot_read_existing_progress(self) -> None:
        client_a = "a" * 64
        client_b = "b" * 64
        request_id = "request-id-123456"
        init_progress(client_a, request_id)
        append_progress(client_a, request_id, "private reasoning")

        response = await main.get_chat_progress(request_id, client_b, 0, None)

        self.assertEqual(response["status"], "missing")
        self.assertEqual(response["events"], [])
        self.assertTrue(response["done"])

    async def test_completed_progress_expires_after_retention_period(self) -> None:
        client_id = "a" * 64
        request_id = "request-id-123456"
        init_progress(client_id, request_id)
        append_progress(client_id, request_id, "private reasoning")

        with patch("progress.time.monotonic", return_value=100.0):
            finish_progress(client_id, request_id)

        before_expiry = 100.0 + progress.PROGRESS_RETENTION_SECONDS - 0.001
        removed = progress.cleanup_expired_progress(now_monotonic=before_expiry)
        self.assertEqual(removed, 0)
        self.assertIn((client_id, request_id), progress_records)

        removed = progress.cleanup_expired_progress(
            now_monotonic=100.0 + progress.PROGRESS_RETENTION_SECONDS
        )
        self.assertEqual(removed, 1)
        self.assertNotIn((client_id, request_id), progress_records)

    async def test_active_progress_is_not_removed_by_ttl_cleanup(self) -> None:
        client_id = "a" * 64
        request_id = "request-id-123456"
        init_progress(client_id, request_id)

        removed = progress.cleanup_expired_progress(now_monotonic=10**12)

        self.assertEqual(removed, 0)
        self.assertIn((client_id, request_id), progress_records)

    async def test_repeated_finish_does_not_extend_progress_retention(self) -> None:
        client_id = "a" * 64
        request_id = "request-id-123456"
        init_progress(client_id, request_id)

        with patch("progress.time.monotonic", side_effect=[100.0, 200.0]):
            finish_progress(client_id, request_id)
            finish_progress(client_id, request_id)

        removed = progress.cleanup_expired_progress(
            now_monotonic=100.0 + progress.PROGRESS_RETENTION_SECONDS
        )

        self.assertEqual(removed, 1)
        self.assertNotIn((client_id, request_id), progress_records)

    async def test_single_progress_event_is_truncated_to_byte_limit_with_notice(self) -> None:
        client_id = "a" * 64
        request_id = "request-id-123456"
        init_progress(client_id, request_id)

        original = "界" * progress.MAX_PROGRESS_EVENT_BYTES
        append_progress(client_id, request_id, original)
        response = await main.get_chat_progress(request_id, client_id, 0, None)
        bounded = response["events"][-1]["message"]

        self.assertNotEqual(bounded, original)
        self.assertTrue(bounded.endswith(progress.PROGRESS_TRUNCATION_SUFFIX))
        self.assertLessEqual(
            len(bounded.encode("utf-8")),
            progress.MAX_PROGRESS_EVENT_BYTES,
        )

    async def test_progress_event_cache_has_a_total_byte_budget(self) -> None:
        client_id = "a" * 64
        request_id = "request-id-123456"
        init_progress(client_id, request_id)
        event_size = progress.MAX_PROGRESS_EVENT_BYTES
        event_count = progress.MAX_PROGRESS_TOTAL_BYTES // event_size + 2

        for index in range(event_count):
            prefix = f"event-{index}:"
            append_progress(
                client_id,
                request_id,
                prefix + "x" * (event_size - len(prefix)),
            )

        response = await main.get_chat_progress(request_id, client_id, 0, None)
        messages = [event["message"] for event in response["events"]]

        self.assertLessEqual(
            sum(len(message.encode("utf-8")) for message in messages),
            progress.MAX_PROGRESS_TOTAL_BYTES,
        )
        self.assertNotIn("Agent 已接收请求，正在准备执行。", messages)
        self.assertTrue(messages[-1].startswith(f"event-{event_count - 1}:"))

    async def test_app_lifespan_starts_and_stops_progress_cleanup(self) -> None:
        cleanup_started = asyncio.Event()
        cleanup_stopped = asyncio.Event()

        async def fake_cleanup() -> None:
            cleanup_started.set()
            try:
                await asyncio.Future()
            finally:
                cleanup_stopped.set()

        dialog_service = SimpleNamespace(start=AsyncMock(), stop=AsyncMock())
        webui_dialog_service = SimpleNamespace(start=AsyncMock(), stop=AsyncMock())
        with (
            patch.object(main, "run_progress_cleanup", fake_cleanup),
            patch.object(main, "dialog_interaction_service", dialog_service),
            patch.object(main, "webui_dialog_interaction_service", webui_dialog_service),
        ):
            async with main.lifespan(main.app):
                await asyncio.wait_for(cleanup_started.wait(), timeout=1)

        self.assertTrue(cleanup_stopped.is_set())
        dialog_service.start.assert_awaited_once_with()
        dialog_service.stop.assert_awaited_once_with()
        webui_dialog_service.start.assert_awaited_once_with()
        webui_dialog_service.stop.assert_awaited_once_with()


if __name__ == "__main__":
    unittest.main()
