from __future__ import annotations

import os
import unittest
from unittest.mock import AsyncMock, patch

os.environ.setdefault("SESSION_HMAC_SECRET", "session-secret-32-bytes-for-tests!!")
os.environ.setdefault("GATEWAY_BRIDGE_TOKEN", "gateway-token-32-bytes-for-tests!!!")

import main
from models import ChatMessage


class LocalGatewayRoutingTests(unittest.IsolatedAsyncioTestCase):
    async def test_communication_uses_explicit_port_without_profile_aliases(self):
        for port, stream in ((8641, False), (8641, True), (8642, False), (19000, True)):
            with self.subTest(port=port, stream=stream):
                async def events(*args):
                    yield b'data: {"output_text":"qa"}\n\n'

                with patch.object(main, "proxy_stream", side_effect=events) as streaming, patch.object(
                    main, "request_json", AsyncMock(return_value={"output_text": "qa"})
                ) as non_streaming:
                    response = await main.communicate(main.CommunicateRequest(
                        agent_port=port,
                        messages=[ChatMessage(role="user", content="问题")],
                        api_key="qa-test-key",
                        stream=stream,
                    ))
                    called = streaming if stream else non_streaming
                    self.assertEqual(called.call_args.args[0], f"http://127.0.0.1:{port}/v1/responses")
                    self.assertEqual(called.call_args.args[2], "qa-test-key")
                    if stream:
                        chunks = [chunk async for chunk in response.body_iterator]
                        self.assertEqual(chunks, [b'data: {"output_text":"qa"}\n\n'])


if __name__ == "__main__":
    unittest.main()
