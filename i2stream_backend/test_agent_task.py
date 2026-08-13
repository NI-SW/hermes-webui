from __future__ import annotations

import unittest
from unittest.mock import AsyncMock

from pydantic import SecretStr

from agent_task import AgentTaskError, run_agent_prompt
from gateway_bridge import PendingGatewayRequest


class FakeBridge:
    def __init__(self, events: list[dict[str, object]]) -> None:
        self.events = events
        self.pending: PendingGatewayRequest | None = None
        self.start_calls: list[tuple[str, dict[str, object], str]] = []
        self.release_request = AsyncMock()

    async def start_request(
        self,
        session_id: str,
        payload: dict[str, object],
        owner_client_id: str,
    ) -> PendingGatewayRequest:
        self.start_calls.append((session_id, payload, owner_client_id))
        self.pending = PendingGatewayRequest(
            request_id="request-1",
            session_id=session_id,
            owner_client_id=owner_client_id,
        )
        return self.pending

    async def iter_events(self, pending: PendingGatewayRequest):
        for event in self.events:
            if event["type"] in {"request.completed", "request.failed"}:
                pending.terminal_received = True
            yield event


class AgentTaskTests(unittest.IsolatedAsyncioTestCase):
    secret = SecretStr("session-secret-32-bytes-for-tests!!")

    async def test_completed_task_uses_isolated_session_and_returns_final_text(self) -> None:
        bridge = FakeBridge([{"type": "request.completed", "text": "{\"name\":\"test\"}"}])

        result = await run_agent_prompt(
            bridge,
            self.secret,
            task_id="task-1",
            prompt="summarize this conversation",
        )

        self.assertEqual(result, '{"name":"test"}')
        session_id, payload, owner_id = bridge.start_calls[0]
        self.assertRegex(session_id, r"^[0-9a-f]{64}$")
        self.assertRegex(owner_id, r"^[0-9a-f]{64}$")
        self.assertNotEqual(session_id, owner_id)
        self.assertEqual(payload, {"input": "summarize this conversation"})
        bridge.release_request.assert_awaited_once_with(
            bridge.pending,
            notify_gateway=False,
        )

    async def test_interactive_or_failed_agent_task_fails_and_cancels(self) -> None:
        cases = (
            ([{"type": "clarify.request"}], "cannot request clarification"),
            ([{"type": "approval.request"}], "cannot request approval"),
            ([{"type": "request.failed", "error": "internal detail"}], "Agent task failed"),
            ([{"type": "request.completed", "text": "   "}], "empty final text"),
        )
        for events, expected_error in cases:
            with self.subTest(events=events):
                bridge = FakeBridge(events)
                with self.assertRaisesRegex(AgentTaskError, expected_error):
                    await run_agent_prompt(
                        bridge,
                        self.secret,
                        task_id="task-2",
                        prompt="summarize",
                    )
                bridge.release_request.assert_awaited_once_with(
                    bridge.pending,
                    notify_gateway=not bool(bridge.pending and bridge.pending.terminal_received),
                )


if __name__ == "__main__":
    unittest.main()
