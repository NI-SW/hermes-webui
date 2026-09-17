from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from fastapi import HTTPException

os.environ.setdefault("SESSION_HMAC_SECRET", "session-secret-32-bytes-for-tests!!")
os.environ.setdefault("GATEWAY_BRIDGE_TOKEN", "gateway-token-32-bytes-for-tests!!!")

import main


class PrepareAgentRequestTests(unittest.TestCase):
    def setUp(self) -> None:
        main.inbox_file_records.clear()

    def tearDown(self) -> None:
        main.inbox_file_records.clear()

    def test_report_token_adds_only_resolved_path_to_agent_payload(self) -> None:
        token = "a" * 48
        visible_prompt = "请根据选中的文档中的解决方案对出现的问题进行处理。"

        with tempfile.TemporaryDirectory() as temp_dir:
            store_dir = Path(temp_dir)
            report_path = store_dir / f"{token}-report.html"
            report_path.write_text("<html>report</html>", encoding="utf-8")
            payload = {
                "model": "hermes-agent",
                "input": visible_prompt,
                "conversation": "conversation-1",
                "report_token": token,
                "store": True,
                "stream": True,
            }

            with patch.object(main.settings, "inbox_file_store_dir", store_dir):
                agent_payload, user_text, user_payload = main.prepare_agent_request(payload)

        self.assertEqual(user_text, visible_prompt)
        self.assertEqual(user_payload, {"report_token": token})
        self.assertNotIn("report_token", agent_payload)
        self.assertEqual(
            agent_payload["input"],
            f"{visible_prompt}\n\n文档路径为：{report_path.resolve()}",
        )
        self.assertEqual(payload["input"], visible_prompt)
        self.assertEqual(payload["report_token"], token)

    def test_request_without_report_token_is_forwarded_without_user_payload(self) -> None:
        payload = {
            "model": "hermes-agent",
            "input": "普通消息",
            "conversation": "conversation-1",
            "store": True,
            "stream": True,
        }

        agent_payload, user_text, user_payload = main.prepare_agent_request(payload)

        self.assertEqual(agent_payload, payload)
        self.assertIsNot(agent_payload, payload)
        self.assertEqual(user_text, "普通消息")
        self.assertIsNone(user_payload)

    def test_invalid_report_token_is_rejected(self) -> None:
        payload = {"input": "处理报告", "report_token": "../report.html"}

        with self.assertRaises(HTTPException) as raised:
            main.prepare_agent_request(payload)

        self.assertEqual(raised.exception.status_code, 400)
        self.assertEqual(raised.exception.detail, "Invalid report token")

    def test_missing_report_file_is_rejected(self) -> None:
        token = "b" * 48
        payload = {"input": "处理报告", "report_token": token}

        with tempfile.TemporaryDirectory() as temp_dir:
            with patch.object(main.settings, "inbox_file_store_dir", Path(temp_dir)):
                with self.assertRaises(HTTPException) as raised:
                    main.prepare_agent_request(payload)

        self.assertEqual(raised.exception.status_code, 404)
        self.assertEqual(raised.exception.detail, "Report file not found")


class PersistedProxyStreamTests(unittest.IsolatedAsyncioTestCase):
    async def test_user_message_saves_report_token_payload(self) -> None:
        inserted_message = Mock()

        async def empty_proxy_stream(*args, **kwargs):
            if False:
                yield b""

        with (
            patch.object(main, "insert_chat_message", inserted_message),
            patch.object(main, "proxy_stream", empty_proxy_stream),
        ):
            chunks = [
                chunk
                async for chunk in main.persisted_proxy_stream(
                    "http://agent/v1/responses",
                    {"input": "处理报告"},
                    "api-key",
                    "client-id",
                    "conversation-id",
                    "处理报告",
                    {"report_token": "a" * 48},
                )
            ]

        self.assertEqual(chunks, [])
        inserted_message.assert_called_once_with(
            "client-id",
            "conversation-id",
            "user",
            "处理报告",
            {"report_token": "a" * 48},
        )


if __name__ == "__main__":
    unittest.main()
