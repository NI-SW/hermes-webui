from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from fastapi import HTTPException, WebSocketDisconnect
from pydantic import SecretStr, ValidationError

os.environ.setdefault("VECTOR_SEARCH_HOST", "http://127.0.0.1:8900")
os.environ.setdefault("SESSION_HMAC_SECRET", "session-secret-32-bytes-for-tests!!")
os.environ.setdefault("GATEWAY_BRIDGE_TOKEN", "gateway-token-32-bytes-for-tests!!!")

import main
from file_state import file_records
from gateway_bridge import (
    GatewayApprovalConflictError,
    GatewayBridge,
    GatewayClarificationConflictError,
    GatewayClarificationNotFoundError,
    GatewayProtocolError,
    GatewaySessionBusyError,
    GatewayUnavailableError,
    MAX_ASSISTANT_TEXT_BYTES,
    MAX_GATEWAY_FRAME_BYTES,
    MAX_REASONING_TEXT_BYTES,
    PendingGatewayRequest,
    derive_gateway_session_id,
    valid_gateway_authorization,
)
from sse_proxy import parse_sse_data


class FakeGatewayWebSocket:
    def __init__(self) -> None:
        self.sent: list[dict[str, object]] = []
        self.headers: dict[str, str] = {}
        self.accepted = False
        self.closed: tuple[int, str] | None = None

    async def send_json(self, payload: dict[str, object]) -> None:
        if not self.accepted:
            raise RuntimeError("WebSocket must be accepted before send_json")
        self.sent.append(payload)

    async def accept(self) -> None:
        self.accepted = True

    async def receive_json(self) -> dict[str, object]:
        raise WebSocketDisconnect(code=1000)

    async def close(self, code: int, reason: str) -> None:
        self.closed = (code, reason)


class FakeRequest:
    def __init__(self, payload: dict[str, object], headers: dict[str, str]) -> None:
        self._payload = payload
        self.headers = headers

    async def json(self) -> dict[str, object]:
        return self._payload.copy()


class SessionDerivationTests(unittest.TestCase):
    def test_session_id_uses_versioned_canonical_json_hmac_sha256(self) -> None:
        secret = SecretStr("x" * 32)
        expected_message = json.dumps(
            ["v1", "a" * 64, "conversation-1"],
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        expected = hmac.new(b"x" * 32, expected_message, hashlib.sha256).hexdigest()

        actual = derive_gateway_session_id("a" * 64, "conversation-1", secret)

        self.assertEqual(actual, expected)

    def test_session_id_separates_clients_and_ambiguous_string_pairs(self) -> None:
        secret = SecretStr("x" * 32)

        first = derive_gateway_session_id("ab", "c", secret)
        second = derive_gateway_session_id("a", "bc", secret)
        other_client = derive_gateway_session_id("d" * 64, "conversation-1", secret)

        self.assertNotEqual(first, second)
        self.assertNotEqual(
            other_client,
            derive_gateway_session_id("e" * 64, "conversation-1", secret),
        )

    def test_gateway_bearer_token_is_compared_without_exposing_secret(self) -> None:
        token = SecretStr("gateway-token-32-bytes-for-tests!!!")

        self.assertTrue(
            valid_gateway_authorization(
                "Bearer gateway-token-32-bytes-for-tests!!!",
                token,
            )
        )
        self.assertFalse(valid_gateway_authorization("Bearer wrong-token", token))
        self.assertFalse(valid_gateway_authorization(None, token))


class GatewayBridgeTests(unittest.IsolatedAsyncioTestCase):
    async def _wait_for_clarify_response_frame(
        self,
        websocket: FakeGatewayWebSocket,
    ) -> dict[str, object]:
        for _ in range(100):
            responses = [
                frame
                for frame in websocket.sent
                if frame["type"] == "clarify.respond"
            ]
            if responses:
                return responses[-1]
            await asyncio.sleep(0)
        self.fail("clarify.respond frame was not sent")

    async def _ack_clarification_response(
        self,
        bridge: GatewayBridge,
        websocket: FakeGatewayWebSocket,
        pending: PendingGatewayRequest,
        clarify_id: str,
        *,
        accepted: bool = True,
    ) -> None:
        await self._wait_for_clarify_response_frame(websocket)
        await bridge.dispatch_event(
            {
                "type": "clarify.response",
                "request_id": pending.request_id,
                "session_id": pending.session_id,
                "clarify_id": clarify_id,
                "accepted": accepted,
            }
        )

    async def _wait_for_approval_response_frame(
        self,
        websocket: FakeGatewayWebSocket,
    ) -> dict[str, object]:
        for _ in range(100):
            responses = [
                frame
                for frame in websocket.sent
                if frame["type"] == "approval.respond"
            ]
            if responses:
                return responses[-1]
            await asyncio.sleep(0)
        self.fail("approval.respond frame was not sent")

    async def test_approval_responses_are_owned_acknowledged_and_fifo(self) -> None:
        bridge = GatewayBridge(request_timeout_seconds=10)
        websocket = FakeGatewayWebSocket()
        await websocket.accept()
        await bridge.attach(websocket)
        owner_client_id = "c" * 64
        pending = await bridge.start_request(
            "a" * 64,
            {"input": "hello"},
            owner_client_id=owner_client_id,
        )
        approvals = [
            ("approval-first", "rm -f /tmp/one", "删除第一个文件"),
            ("approval-second", "rm -f /tmp/two", "删除第二个文件"),
        ]
        for approval_id, command, description in approvals:
            await bridge.dispatch_event(
                {
                    "type": "approval.request",
                    "request_id": pending.request_id,
                    "session_id": pending.session_id,
                    "approval_id": approval_id,
                    "command": command,
                    "description": description,
                    "allow_permanent": True,
                    "allow_session": True,
                    "smart_denied": False,
                }
            )

        with self.assertRaises(main.GatewayApprovalNotFoundError):
            await bridge.respond_approval(
                pending.request_id,
                approvals[0][0],
                "d" * 64,
                "once",
            )
        with self.assertRaises(main.GatewayApprovalConflictError):
            await bridge.respond_approval(
                pending.request_id,
                approvals[1][0],
                owner_client_id,
                "once",
            )

        first_response = asyncio.create_task(
            bridge.respond_approval(
                pending.request_id,
                approvals[0][0],
                owner_client_id,
                "session",
            )
        )
        first_frame = await self._wait_for_approval_response_frame(websocket)
        self.assertEqual(
            first_frame,
            {
                "type": "approval.respond",
                "request_id": pending.request_id,
                "session_id": pending.session_id,
                "approval_id": approvals[0][0],
                "choice": "session",
            },
        )
        await bridge.dispatch_event(
            {
                "type": "approval.response",
                "request_id": pending.request_id,
                "session_id": pending.session_id,
                "approval_id": approvals[0][0],
                "accepted": True,
            }
        )
        await first_response

        second_response = asyncio.create_task(
            bridge.respond_approval(
                pending.request_id,
                approvals[1][0],
                owner_client_id,
                "deny",
            )
        )
        for _ in range(100):
            frames = [
                frame
                for frame in websocket.sent
                if frame["type"] == "approval.respond"
            ]
            if len(frames) == 2:
                break
            await asyncio.sleep(0)
        else:
            self.fail("second approval.respond frame was not sent")
        await bridge.dispatch_event(
            {
                "type": "approval.response",
                "request_id": pending.request_id,
                "session_id": pending.session_id,
                "approval_id": approvals[1][0],
                "accepted": True,
            }
        )
        await second_response

        self.assertEqual([frame["choice"] for frame in frames], ["session", "deny"])
        self.assertNotIn("client_id", json.dumps(frames))
        with self.assertRaises(main.GatewayApprovalConflictError):
            await bridge.respond_approval(
                pending.request_id,
                approvals[1][0],
                owner_client_id,
                "once",
            )
        await bridge.release_request(pending, notify_gateway=False)

    async def test_approval_protocol_rejects_invalid_choice_and_malformed_frames(self) -> None:
        bridge = GatewayBridge(request_timeout_seconds=10)
        websocket = FakeGatewayWebSocket()
        await websocket.accept()
        await bridge.attach(websocket)
        pending = await bridge.start_request(
            "a" * 64,
            {"input": "hello"},
            owner_client_id="c" * 64,
        )
        base_frame = {
            "type": "approval.request",
            "request_id": pending.request_id,
            "session_id": pending.session_id,
            "approval_id": "approval-1",
            "command": "rm -f /tmp/example",
            "description": "删除临时文件",
            "allow_permanent": True,
            "allow_session": True,
            "smart_denied": False,
        }
        for invalid_frame in (
            {**base_frame, "approval_id": ""},
            {**base_frame, "command": ""},
            {**base_frame, "description": ""},
            {**base_frame, "description": 1},
            {key: value for key, value in base_frame.items() if key != "allow_session"},
            {**base_frame, "allow_permanent": 1},
            {**base_frame, "allow_session": None},
            {**base_frame, "smart_denied": "false"},
        ):
            with self.assertRaises(GatewayProtocolError):
                await bridge.dispatch_event(invalid_frame)

        await bridge.dispatch_event(base_frame)
        with self.assertRaises(GatewayProtocolError):
            await bridge.respond_approval(
                pending.request_id,
                base_frame["approval_id"],
                "c" * 64,
                "later",
            )
        await bridge.release_request(pending, notify_gateway=False)

    async def test_approval_rejects_choices_disabled_by_hermes(self) -> None:
        bridge = GatewayBridge(request_timeout_seconds=10)
        websocket = FakeGatewayWebSocket()
        await websocket.accept()
        await bridge.attach(websocket)
        pending = await bridge.start_request(
            "a" * 64,
            {"input": "hello"},
            owner_client_id="c" * 64,
        )
        await bridge.dispatch_event(
            {
                "type": "approval.request",
                "request_id": pending.request_id,
                "session_id": pending.session_id,
                "approval_id": "approval-smart-denied",
                "command": "rm -f /tmp/example",
                "description": "删除临时文件",
                "allow_permanent": True,
                "allow_session": True,
                "smart_denied": True,
            }
        )

        for choice in ("session", "always"):
            with self.assertRaisesRegex(
                GatewayApprovalConflictError,
                "not allowed",
            ):
                await bridge.respond_approval(
                    pending.request_id,
                    "approval-smart-denied",
                    "c" * 64,
                    choice,
                )

        self.assertFalse(
            any(
                frame["type"] == "approval.respond"
                for frame in websocket.sent
            )
        )
        await bridge.release_request(pending, notify_gateway=False)

    async def test_approval_ack_timeout_clears_waiter_and_ignores_late_ack(self) -> None:
        bridge = GatewayBridge(
            request_timeout_seconds=10,
            approval_ack_timeout_seconds=0.01,
        )
        websocket = FakeGatewayWebSocket()
        await websocket.accept()
        await bridge.attach(websocket)
        pending = await bridge.start_request(
            "a" * 64,
            {"input": "hello"},
            owner_client_id="c" * 64,
        )
        approval_id = "approval-ack-timeout"
        await bridge.dispatch_event(
            {
                "type": "approval.request",
                "request_id": pending.request_id,
                "session_id": pending.session_id,
                "approval_id": approval_id,
                "command": "rm -f /tmp/example",
                "description": "删除临时文件",
                "allow_permanent": True,
                "allow_session": True,
                "smart_denied": False,
            }
        )

        with self.assertRaises(main.GatewayApprovalConflictError):
            await bridge.respond_approval(
                pending.request_id,
                approval_id,
                "c" * 64,
                "once",
            )

        self.assertEqual(pending.pending_approvals, [])
        self.assertIsNone(pending.approval_response_waiter)
        await bridge.dispatch_event(
            {
                "type": "approval.response",
                "request_id": pending.request_id,
                "session_id": pending.session_id,
                "approval_id": approval_id,
                "accepted": True,
            }
        )
        await bridge.release_request(pending, notify_gateway=False)

    async def test_approval_wait_uses_dedicated_timeout_and_releases_request(self) -> None:
        bridge = GatewayBridge(
            request_timeout_seconds=10,
            approval_timeout_seconds=0.01,
        )
        websocket = FakeGatewayWebSocket()
        await websocket.accept()
        await bridge.attach(websocket)
        pending = await bridge.start_request(
            "a" * 64,
            {"input": "hello"},
            owner_client_id="c" * 64,
        )
        await bridge.dispatch_event(
            {
                "type": "approval.request",
                "request_id": pending.request_id,
                "session_id": pending.session_id,
                "approval_id": "approval-wait-timeout",
                "command": "rm -f /tmp/example",
                "description": "删除临时文件",
                "allow_permanent": True,
                "allow_session": True,
                "smart_denied": False,
            }
        )

        events = [event async for event in bridge.iter_events(pending)]

        self.assertEqual(events[0]["type"], "approval.request")
        self.assertEqual(events[1]["type"], "request.failed")
        self.assertEqual(
            events[1]["error"],
            "Hermes Gateway approval timed out",
        )
        self.assertEqual(pending.pending_approvals, [])
        self.assertEqual(websocket.sent[-1]["type"], "request.cancel")

    async def test_each_fifo_approval_gets_a_fresh_wait_timeout(self) -> None:
        bridge = GatewayBridge(
            request_timeout_seconds=10,
            approval_timeout_seconds=0.2,
        )
        websocket = FakeGatewayWebSocket()
        await websocket.accept()
        await bridge.attach(websocket)
        pending = await bridge.start_request(
            "a" * 64,
            {"input": "hello"},
            owner_client_id="c" * 64,
        )
        for approval_id in ("approval-timeout-first", "approval-timeout-second"):
            await bridge.dispatch_event(
                {
                    "type": "approval.request",
                    "request_id": pending.request_id,
                    "session_id": pending.session_id,
                    "approval_id": approval_id,
                    "command": f"echo {approval_id}",
                    "description": f"执行 {approval_id}",
                    "allow_permanent": True,
                    "allow_session": True,
                    "smart_denied": False,
                }
            )
        events = bridge.iter_events(pending)
        self.assertEqual((await anext(events))["approval_id"], "approval-timeout-first")
        self.assertEqual((await anext(events))["approval_id"], "approval-timeout-second")
        timeout_event_task = asyncio.create_task(anext(events))

        await asyncio.sleep(0.15)
        response_task = asyncio.create_task(
            bridge.respond_approval(
                pending.request_id,
                "approval-timeout-first",
                "c" * 64,
                "once",
            )
        )
        await self._wait_for_approval_response_frame(websocket)
        await bridge.dispatch_event(
            {
                "type": "approval.response",
                "request_id": pending.request_id,
                "session_id": pending.session_id,
                "approval_id": "approval-timeout-first",
                "accepted": True,
            }
        )
        await response_task

        await asyncio.sleep(0.1)
        self.assertFalse(timeout_event_task.done())
        failure = await asyncio.wait_for(timeout_event_task, timeout=0.15)
        self.assertEqual(failure["error"], "Hermes Gateway approval timed out")

    async def test_terminal_event_unblocks_pending_approval_response(self) -> None:
        bridge = GatewayBridge(request_timeout_seconds=10)
        websocket = FakeGatewayWebSocket()
        await websocket.accept()
        await bridge.attach(websocket)
        pending = await bridge.start_request(
            "a" * 64,
            {"input": "hello"},
            owner_client_id="c" * 64,
        )
        approval_id = "approval-terminal"
        await bridge.dispatch_event(
            {
                "type": "approval.request",
                "request_id": pending.request_id,
                "session_id": pending.session_id,
                "approval_id": approval_id,
                "command": "rm -f /tmp/example",
                "description": "删除临时文件",
                "allow_permanent": True,
                "allow_session": True,
                "smart_denied": False,
            }
        )
        response_task = asyncio.create_task(
            bridge.respond_approval(
                pending.request_id,
                approval_id,
                "c" * 64,
                "once",
            )
        )
        await self._wait_for_approval_response_frame(websocket)

        await bridge.dispatch_event(
            {
                "type": "request.failed",
                "request_id": pending.request_id,
                "session_id": pending.session_id,
                "error": "Agent stopped",
            }
        )

        with self.assertRaises(main.GatewayApprovalConflictError):
            await response_task
        self.assertEqual(pending.pending_approvals, [])
        self.assertIsNone(pending.approval_response_waiter)
        await bridge.release_request(pending, notify_gateway=False)

    async def test_cancelled_approval_response_expires_after_ack_window(self) -> None:
        bridge = GatewayBridge(
            request_timeout_seconds=10,
            approval_ack_timeout_seconds=0.01,
        )
        websocket = FakeGatewayWebSocket()
        await websocket.accept()
        await bridge.attach(websocket)
        pending = await bridge.start_request(
            "a" * 64,
            {"input": "hello"},
            owner_client_id="c" * 64,
        )
        approval_id = "approval-cancelled"
        await bridge.dispatch_event(
            {
                "type": "approval.request",
                "request_id": pending.request_id,
                "session_id": pending.session_id,
                "approval_id": approval_id,
                "command": "rm -f /tmp/example",
                "description": "删除临时文件",
                "allow_permanent": True,
                "allow_session": True,
                "smart_denied": False,
            }
        )
        response_task = asyncio.create_task(
            bridge.respond_approval(
                pending.request_id,
                approval_id,
                "c" * 64,
                "once",
            )
        )
        await self._wait_for_approval_response_frame(websocket)

        response_task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await response_task

        self.assertEqual(pending.pending_approvals, [])
        self.assertIsNone(pending.approval_response_waiter)
        await bridge.release_request(pending, notify_gateway=False)

    async def test_disconnect_unblocks_pending_approval_response(self) -> None:
        bridge = GatewayBridge(request_timeout_seconds=10)
        websocket = FakeGatewayWebSocket()
        await websocket.accept()
        await bridge.attach(websocket)
        pending = await bridge.start_request(
            "a" * 64,
            {"input": "hello"},
            owner_client_id="c" * 64,
        )
        approval_id = "approval-disconnect"
        await bridge.dispatch_event(
            {
                "type": "approval.request",
                "request_id": pending.request_id,
                "session_id": pending.session_id,
                "approval_id": approval_id,
                "command": "rm -f /tmp/example",
                "description": "删除临时文件",
                "allow_permanent": True,
                "allow_session": True,
                "smart_denied": False,
            }
        )
        response_task = asyncio.create_task(
            bridge.respond_approval(
                pending.request_id,
                approval_id,
                "c" * 64,
                "once",
            )
        )
        await self._wait_for_approval_response_frame(websocket)

        await bridge.detach(websocket, "Gateway bridge disconnected")

        with self.assertRaises(GatewayUnavailableError):
            await response_task
        self.assertEqual(pending.pending_approvals, [])
        self.assertIsNone(pending.approval_response_waiter)

    async def test_clarify_response_is_owned_one_shot_and_does_not_leak_client_id(self) -> None:
        bridge = GatewayBridge(request_timeout_seconds=10)
        websocket = FakeGatewayWebSocket()
        await websocket.accept()
        await bridge.attach(websocket)
        pending = await bridge.start_request(
            "a" * 64,
            {"input": "hello"},
            owner_client_id="c" * 64,
        )
        clarify_id = "clarify-" + "1" * 24
        await bridge.dispatch_event(
            {
                "type": "clarify.request",
                "request_id": pending.request_id,
                "session_id": pending.session_id,
                "clarify_id": clarify_id,
                "question": "是否停止规则？",
                "choices": ["仅停止同步", "同时停止解析"],
            }
        )

        event = pending.queue.get_nowait()
        self.assertEqual(event["clarify_id"], clarify_id)
        self.assertEqual(event["question"], "是否停止规则？")
        response_task = asyncio.create_task(
            bridge.respond_clarify(
                pending.request_id,
                clarify_id,
                "c" * 64,
                "仅停止同步",
            )
        )
        await self._ack_clarification_response(
            bridge,
            websocket,
            pending,
            clarify_id,
        )
        await response_task

        response_frame = websocket.sent[-1]
        self.assertEqual(
            response_frame,
            {
                "type": "clarify.respond",
                "request_id": pending.request_id,
                "session_id": pending.session_id,
                "clarify_id": clarify_id,
                "response": "仅停止同步",
            },
        )
        self.assertNotIn("client_id", json.dumps(response_frame))
        with self.assertRaises(GatewayClarificationConflictError):
            await bridge.respond_clarify(
                pending.request_id,
                clarify_id,
                "c" * 64,
                "同时停止解析",
            )
        self.assertEqual(
            sum(frame["type"] == "clarify.respond" for frame in websocket.sent),
            1,
        )
        self.assertEqual(pending.queue.qsize(), 0)
        self.assertIsNone(pending.clarification_response_waiter)
        await bridge.release_request(pending, notify_gateway=False)

    async def test_clarify_response_rejects_other_client_without_consuming_prompt(self) -> None:
        bridge = GatewayBridge(request_timeout_seconds=10)
        websocket = FakeGatewayWebSocket()
        await websocket.accept()
        await bridge.attach(websocket)
        pending = await bridge.start_request(
            "a" * 64,
            {"input": "hello"},
            owner_client_id="c" * 64,
        )
        clarify_id = "clarify-" + "2" * 24
        await bridge.dispatch_event(
            {
                "type": "clarify.request",
                "request_id": pending.request_id,
                "session_id": pending.session_id,
                "clarify_id": clarify_id,
                "question": "请选择操作",
                "choices": [],
            }
        )

        with self.assertRaises(GatewayClarificationNotFoundError):
            await bridge.respond_clarify(
                pending.request_id,
                clarify_id,
                "d" * 64,
                "继续",
            )
        with self.assertRaises(GatewayClarificationConflictError):
            await bridge.respond_clarify(
                pending.request_id,
                "clarify-" + "9" * 24,
                "c" * 64,
                "继续",
            )
        response_task = asyncio.create_task(
            bridge.respond_clarify(
                pending.request_id,
                clarify_id,
                "c" * 64,
                "继续",
            )
        )
        await self._ack_clarification_response(
            bridge,
            websocket,
            pending,
            clarify_id,
        )
        await response_task

        self.assertEqual(websocket.sent[-1]["response"], "继续")
        await bridge.release_request(pending, notify_gateway=False)

    async def test_concurrent_clarify_responses_send_exactly_one_frame(self) -> None:
        bridge = GatewayBridge(request_timeout_seconds=10)
        websocket = FakeGatewayWebSocket()
        await websocket.accept()
        await bridge.attach(websocket)
        pending = await bridge.start_request(
            "a" * 64,
            {"input": "hello"},
            owner_client_id="c" * 64,
        )
        clarify_id = "clarify-" + "7" * 24
        await bridge.dispatch_event(
            {
                "type": "clarify.request",
                "request_id": pending.request_id,
                "session_id": pending.session_id,
                "clarify_id": clarify_id,
                "question": "是否继续？",
                "choices": ["继续", "取消"],
            }
        )

        first_response = asyncio.create_task(
            bridge.respond_clarify(
                pending.request_id,
                clarify_id,
                "c" * 64,
                "继续",
            )
        )
        second_response = asyncio.create_task(
            bridge.respond_clarify(
                pending.request_id,
                clarify_id,
                "c" * 64,
                "取消",
            )
        )
        await self._ack_clarification_response(
            bridge,
            websocket,
            pending,
            clarify_id,
        )
        results = await asyncio.gather(
            first_response,
            second_response,
            return_exceptions=True,
        )

        self.assertEqual(sum(result is None for result in results), 1)
        self.assertEqual(
            sum(
                isinstance(result, GatewayClarificationConflictError)
                for result in results
            ),
            1,
        )
        self.assertEqual(
            sum(frame["type"] == "clarify.respond" for frame in websocket.sent),
            1,
        )
        await bridge.release_request(pending, notify_gateway=False)

    async def test_rejected_clarify_response_ack_is_reported_as_conflict(self) -> None:
        bridge = GatewayBridge(request_timeout_seconds=10)
        websocket = FakeGatewayWebSocket()
        await websocket.accept()
        await bridge.attach(websocket)
        pending = await bridge.start_request(
            "a" * 64,
            {"input": "hello"},
            owner_client_id="c" * 64,
        )
        clarify_id = "clarify-" + "8" * 24
        await bridge.dispatch_event(
            {
                "type": "clarify.request",
                "request_id": pending.request_id,
                "session_id": pending.session_id,
                "clarify_id": clarify_id,
                "question": "是否继续？",
                "choices": ["继续", "取消"],
            }
        )
        pending.queue.get_nowait()

        response_task = asyncio.create_task(
            bridge.respond_clarify(
                pending.request_id,
                clarify_id,
                "c" * 64,
                "继续",
            )
        )
        await self._ack_clarification_response(
            bridge,
            websocket,
            pending,
            clarify_id,
            accepted=False,
        )

        with self.assertRaisesRegex(
            GatewayClarificationConflictError,
            "rejected",
        ):
            await response_task
        self.assertIsNone(pending.current_clarification)
        self.assertIsNone(pending.clarification_response_waiter)
        self.assertEqual(pending.queue.qsize(), 0)
        await bridge.release_request(pending, notify_gateway=False)

    async def test_clarify_response_ack_timeout_clears_waiter(self) -> None:
        bridge = GatewayBridge(
            request_timeout_seconds=10,
            clarification_ack_timeout_seconds=0.01,
        )
        websocket = FakeGatewayWebSocket()
        await websocket.accept()
        await bridge.attach(websocket)
        pending = await bridge.start_request(
            "a" * 64,
            {"input": "hello"},
            owner_client_id="c" * 64,
        )
        clarify_id = "clarify-" + "9" * 24
        await bridge.dispatch_event(
            {
                "type": "clarify.request",
                "request_id": pending.request_id,
                "session_id": pending.session_id,
                "clarify_id": clarify_id,
                "question": "是否继续？",
                "choices": ["继续"],
            }
        )
        pending.queue.get_nowait()

        with self.assertRaisesRegex(
            GatewayClarificationConflictError,
            "acknowledgement timed out",
        ):
            await bridge.respond_clarify(
                pending.request_id,
                clarify_id,
                "c" * 64,
                "继续",
            )

        self.assertIsNone(pending.current_clarification)
        self.assertIsNone(pending.clarification_response_waiter)
        await bridge.dispatch_event(
            {
                "type": "clarify.response",
                "request_id": pending.request_id,
                "session_id": pending.session_id,
                "clarify_id": clarify_id,
                "accepted": True,
            }
        )
        self.assertEqual(pending.queue.qsize(), 0)
        await bridge.release_request(pending, notify_gateway=False)

    async def test_cancelled_response_before_send_lock_clears_waiter(self) -> None:
        bridge = GatewayBridge(request_timeout_seconds=10)
        websocket = FakeGatewayWebSocket()
        await websocket.accept()
        await bridge.attach(websocket)
        pending = await bridge.start_request(
            "a" * 64,
            {"input": "hello"},
            owner_client_id="c" * 64,
        )
        clarify_id = "clarify-before-send"
        await bridge.dispatch_event(
            {
                "type": "clarify.request",
                "request_id": pending.request_id,
                "session_id": pending.session_id,
                "clarify_id": clarify_id,
                "question": "是否继续？",
                "choices": [],
            }
        )
        pending.queue.get_nowait()

        await bridge._send_lock.acquire()
        try:
            response_task = asyncio.create_task(
                bridge.respond_clarify(
                    pending.request_id,
                    clarify_id,
                    "c" * 64,
                    "继续",
                )
            )
            await asyncio.sleep(0)
            response_task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await response_task
        finally:
            bridge._send_lock.release()

        self.assertIsNone(pending.current_clarification)
        self.assertIsNone(pending.clarification_response_waiter)
        self.assertFalse(
            any(frame["type"] == "clarify.respond" for frame in websocket.sent)
        )
        await bridge.release_request(pending, notify_gateway=False)

    async def test_cancelled_response_waits_for_inflight_ack_before_cleanup(self) -> None:
        bridge = GatewayBridge(
            request_timeout_seconds=10,
            clarification_ack_timeout_seconds=0.1,
        )
        websocket = FakeGatewayWebSocket()
        await websocket.accept()
        await bridge.attach(websocket)
        pending = await bridge.start_request(
            "a" * 64,
            {"input": "hello"},
            owner_client_id="c" * 64,
        )
        clarify_id = "clarify-cancelled"
        await bridge.dispatch_event(
            {
                "type": "clarify.request",
                "request_id": pending.request_id,
                "session_id": pending.session_id,
                "clarify_id": clarify_id,
                "question": "是否继续？",
                "choices": [],
            }
        )
        pending.queue.get_nowait()
        response_task = asyncio.create_task(
            bridge.respond_clarify(
                pending.request_id,
                clarify_id,
                "c" * 64,
                "继续",
            )
        )
        await self._wait_for_clarify_response_frame(websocket)

        response_task.cancel()
        await asyncio.sleep(0)
        self.assertFalse(response_task.done())
        self.assertIsNotNone(pending.current_clarification)
        self.assertIsNotNone(pending.clarification_response_waiter)
        await bridge.dispatch_event(
            {
                "type": "clarify.response",
                "request_id": pending.request_id,
                "session_id": pending.session_id,
                "clarify_id": clarify_id,
                "accepted": True,
            }
        )

        with self.assertRaises(asyncio.CancelledError):
            await response_task
        self.assertIsNone(pending.current_clarification)
        self.assertIsNone(pending.clarification_response_waiter)
        self.assertTrue(bridge.connected)
        await bridge.release_request(pending, notify_gateway=False)

    async def test_cancelled_response_keeps_send_lock_until_frame_finishes(self) -> None:
        class BlockingGatewayWebSocket(FakeGatewayWebSocket):
            def __init__(self) -> None:
                super().__init__()
                self.clarify_send_started = asyncio.Event()
                self.release_clarify_send = asyncio.Event()
                self.clarify_send_active = False
                self.concurrent_send = False

            async def send_json(self, payload):
                if payload["type"] == "clarify.respond":
                    self.clarify_send_active = True
                    self.clarify_send_started.set()
                    await self.release_clarify_send.wait()
                    self.sent.append(payload)
                    self.clarify_send_active = False
                    return
                if self.clarify_send_active:
                    self.concurrent_send = True
                await super().send_json(payload)

        bridge = GatewayBridge(
            request_timeout_seconds=10,
            clarification_ack_timeout_seconds=0.1,
        )
        websocket = BlockingGatewayWebSocket()
        await websocket.accept()
        await bridge.attach(websocket)
        pending = await bridge.start_request(
            "a" * 64,
            {"input": "first"},
            owner_client_id="c" * 64,
        )
        clarify_id = "clarify-blocking-send"
        await bridge.dispatch_event(
            {
                "type": "clarify.request",
                "request_id": pending.request_id,
                "session_id": pending.session_id,
                "clarify_id": clarify_id,
                "question": "是否继续？",
                "choices": [],
            }
        )
        pending.queue.get_nowait()

        response_task = asyncio.create_task(
            bridge.respond_clarify(
                pending.request_id,
                clarify_id,
                "c" * 64,
                "继续",
            )
        )
        await websocket.clarify_send_started.wait()
        response_task.cancel()
        second_start = asyncio.create_task(
            bridge.start_request(
                "b" * 64,
                {"input": "second"},
                owner_client_id="d" * 64,
            )
        )
        await asyncio.sleep(0)

        self.assertFalse(second_start.done())
        self.assertFalse(websocket.concurrent_send)
        websocket.release_clarify_send.set()
        await self._wait_for_clarify_response_frame(websocket)
        await bridge.dispatch_event(
            {
                "type": "clarify.response",
                "request_id": pending.request_id,
                "session_id": pending.session_id,
                "clarify_id": clarify_id,
                "accepted": True,
            }
        )

        with self.assertRaises(asyncio.CancelledError):
            await response_task
        second = await second_start
        self.assertFalse(websocket.concurrent_send)
        self.assertEqual(
            [frame["type"] for frame in websocket.sent[-2:]],
            ["clarify.respond", "request.start"],
        )
        await bridge.release_request(pending, notify_gateway=False)
        await bridge.release_request(second, notify_gateway=False)

    async def test_disconnect_terminal_and_release_unblock_clarify_response_waiter(self) -> None:
        async def assert_unblocked_by(action: str) -> None:
            bridge = GatewayBridge(request_timeout_seconds=10)
            websocket = FakeGatewayWebSocket()
            await websocket.accept()
            await bridge.attach(websocket)
            pending = await bridge.start_request(
                "a" * 64,
                {"input": action},
                owner_client_id="c" * 64,
            )
            clarify_id = f"clarify-{action}"
            await bridge.dispatch_event(
                {
                    "type": "clarify.request",
                    "request_id": pending.request_id,
                    "session_id": pending.session_id,
                    "clarify_id": clarify_id,
                    "question": "是否继续？",
                    "choices": [],
                }
            )
            pending.queue.get_nowait()
            response_task = asyncio.create_task(
                bridge.respond_clarify(
                    pending.request_id,
                    clarify_id,
                    "c" * 64,
                    "继续",
                )
            )
            await self._wait_for_clarify_response_frame(websocket)

            if action == "disconnect":
                await bridge.detach(websocket, "Gateway bridge disconnected")
                expected_error = GatewayUnavailableError
            elif action == "terminal":
                await bridge.dispatch_event(
                    {
                        "type": "request.failed",
                        "request_id": pending.request_id,
                        "session_id": pending.session_id,
                        "error": "request failed",
                    }
                )
                expected_error = GatewayClarificationConflictError
            else:
                await bridge.release_request(pending, notify_gateway=False)
                expected_error = GatewayClarificationConflictError

            with self.assertRaises(expected_error):
                await response_task
            self.assertIsNone(pending.clarification_response_waiter)

        for action in ("disconnect", "terminal", "release"):
            with self.subTest(action=action):
                await assert_unblocked_by(action)

    async def test_clarify_response_ack_requires_boolean_and_matching_id(self) -> None:
        bridge = GatewayBridge(request_timeout_seconds=10)
        websocket = FakeGatewayWebSocket()
        await websocket.accept()
        await bridge.attach(websocket)
        pending = await bridge.start_request(
            "a" * 64,
            {"input": "hello"},
            owner_client_id="c" * 64,
        )
        clarify_id = "clarify-" + "a" * 24
        await bridge.dispatch_event(
            {
                "type": "clarify.request",
                "request_id": pending.request_id,
                "session_id": pending.session_id,
                "clarify_id": clarify_id,
                "question": "是否继续？",
                "choices": [],
            }
        )
        pending.queue.get_nowait()
        response_task = asyncio.create_task(
            bridge.respond_clarify(
                pending.request_id,
                clarify_id,
                "c" * 64,
                "继续",
            )
        )
        await self._wait_for_clarify_response_frame(websocket)

        for frame in (
            {
                "type": "clarify.response",
                "request_id": pending.request_id,
                "session_id": pending.session_id,
                "clarify_id": clarify_id,
                "accepted": "true",
            },
            {
                "type": "clarify.response",
                "request_id": pending.request_id,
                "session_id": pending.session_id,
                "clarify_id": "clarify-wrong",
                "accepted": True,
            },
        ):
            with self.assertRaises(GatewayProtocolError):
                await bridge.dispatch_event(frame)

        await bridge.dispatch_event(
            {
                "type": "clarify.response",
                "request_id": pending.request_id,
                "session_id": pending.session_id,
                "clarify_id": clarify_id,
                "accepted": True,
            }
        )
        await response_task
        await bridge.release_request(pending, notify_gateway=False)

    async def test_clarification_wait_uses_longer_timeout_than_normal_events(self) -> None:
        bridge = GatewayBridge(
            request_timeout_seconds=0.01,
            clarification_timeout_seconds=0.1,
        )
        websocket = FakeGatewayWebSocket()
        await websocket.accept()
        await bridge.attach(websocket)
        pending = await bridge.start_request(
            "a" * 64,
            {"input": "hello"},
            owner_client_id="c" * 64,
        )
        await bridge.dispatch_event(
            {
                "type": "clarify.request",
                "request_id": pending.request_id,
                "session_id": pending.session_id,
                "clarify_id": "clarify-timeout",
                "question": "是否继续？",
                "choices": [],
            }
        )
        events = bridge.iter_events(pending)
        clarification = await anext(events)
        wait_for_terminal = asyncio.create_task(anext(events))

        await asyncio.sleep(0.03)
        self.assertFalse(wait_for_terminal.done())
        failure = await wait_for_terminal

        self.assertEqual(clarification["type"], "clarify.request")
        self.assertEqual(failure["type"], "request.failed")
        self.assertEqual(failure["error"], "Hermes Gateway clarification timed out")
        with self.assertRaises(StopAsyncIteration):
            await anext(events)

    async def test_normal_event_wait_still_uses_request_timeout(self) -> None:
        bridge = GatewayBridge(
            request_timeout_seconds=0.01,
            clarification_timeout_seconds=1,
        )
        websocket = FakeGatewayWebSocket()
        await websocket.accept()
        await bridge.attach(websocket)
        pending = await bridge.start_request(
            "a" * 64,
            {"input": "hello"},
            owner_client_id="c" * 64,
        )

        events = [event async for event in bridge.iter_events(pending)]

        self.assertEqual(
            events,
            [
                {
                    "type": "request.failed",
                    "request_id": pending.request_id,
                    "session_id": pending.session_id,
                    "error": "Hermes Gateway request timed out",
                }
            ],
        )

    async def test_clarify_request_is_strict_and_terminal_state_clears_it(self) -> None:
        bridge = GatewayBridge(request_timeout_seconds=10)
        websocket = FakeGatewayWebSocket()
        await websocket.accept()
        await bridge.attach(websocket)
        pending = await bridge.start_request(
            "a" * 64,
            {"input": "hello"},
            owner_client_id="c" * 64,
        )
        base_frame = {
            "type": "clarify.request",
            "request_id": pending.request_id,
            "session_id": pending.session_id,
            "clarify_id": "clarify-" + "3" * 24,
            "question": "请选择",
            "choices": ["继续"],
        }
        invalid_frames = (
            {**base_frame, "clarify_id": ""},
            {**base_frame, "clarify_id": "clarify id with spaces"},
            {**base_frame, "question": "   "},
            {**base_frame, "choices": "继续"},
            {**base_frame, "choices": [""]},
        )
        for frame in invalid_frames:
            with self.subTest(frame=frame):
                with self.assertRaises(GatewayProtocolError):
                    await bridge.dispatch_event(frame)

        await bridge.dispatch_event(base_frame)
        await bridge.dispatch_event(
            {
                "type": "request.failed",
                "request_id": pending.request_id,
                "session_id": pending.session_id,
                "error": "cancelled",
            }
        )
        with self.assertRaises(GatewayClarificationConflictError):
            await bridge.respond_clarify(
                pending.request_id,
                base_frame["clarify_id"],
                "c" * 64,
                "继续",
            )
        await bridge.release_request(pending, notify_gateway=False)

    async def test_cancelling_start_during_send_rolls_back_and_sends_cancel(self) -> None:
        class CancellableGatewayWebSocket(FakeGatewayWebSocket):
            def __init__(self) -> None:
                super().__init__()
                self.first_send_started = asyncio.Event()
                self.block_first_send = True

            async def send_json(self, payload):
                if not self.accepted:
                    raise RuntimeError("WebSocket must be accepted before send_json")
                if self.block_first_send:
                    self.block_first_send = False
                    self.first_send_started.set()
                    await asyncio.Event().wait()
                self.sent.append(payload)

        bridge = GatewayBridge(request_timeout_seconds=10)
        websocket = CancellableGatewayWebSocket()
        await websocket.accept()
        await bridge.attach(websocket)
        start_task = asyncio.create_task(
            bridge.start_request(
                "a" * 64,
                {"input": "cancelled"},
                owner_client_id="c" * 64,
            )
        )
        await websocket.first_send_started.wait()
        start_task.cancel()

        with self.assertRaises(asyncio.CancelledError):
            await start_task

        retry = await bridge.start_request(
            "a" * 64,
            {"input": "retry"},
            owner_client_id="c" * 64,
        )
        self.assertEqual(websocket.sent[0]["type"], "request.cancel")
        self.assertEqual(websocket.sent[1]["payload"], {"input": "retry"})
        await bridge.release_request(retry, notify_gateway=False)

    async def test_cancelling_start_while_waiting_for_send_lock_rolls_back_session(self) -> None:
        class BlockingGatewayWebSocket(FakeGatewayWebSocket):
            def __init__(self) -> None:
                super().__init__()
                self.first_send_started = asyncio.Event()
                self.release_first_send = asyncio.Event()
                self.send_count = 0

            async def send_json(self, payload):
                if not self.accepted:
                    raise RuntimeError("WebSocket must be accepted before send_json")
                self.send_count += 1
                if self.send_count == 1:
                    self.first_send_started.set()
                    await self.release_first_send.wait()
                self.sent.append(payload)

        bridge = GatewayBridge(request_timeout_seconds=10)
        websocket = BlockingGatewayWebSocket()
        await websocket.accept()
        await bridge.attach(websocket)
        first_task = asyncio.create_task(
            bridge.start_request(
                "a" * 64,
                {"input": "first"},
                owner_client_id="c" * 64,
            )
        )
        await websocket.first_send_started.wait()
        cancelled_task = asyncio.create_task(
            bridge.start_request(
                "b" * 64,
                {"input": "cancelled"},
                owner_client_id="d" * 64,
            )
        )
        await asyncio.sleep(0)
        cancelled_task.cancel()

        with self.assertRaises(asyncio.CancelledError):
            await cancelled_task

        retry_task = asyncio.create_task(
            bridge.start_request(
                "b" * 64,
                {"input": "retry"},
                owner_client_id="d" * 64,
            )
        )
        await asyncio.sleep(0)
        self.assertFalse(retry_task.done())
        websocket.release_first_send.set()
        first = await first_task
        retry = await retry_task

        self.assertEqual(websocket.sent[-1]["payload"], {"input": "retry"})
        await bridge.release_request(first, notify_gateway=False)
        await bridge.release_request(retry, notify_gateway=False)

    async def test_snapshots_do_not_fill_event_queue_and_complete_uses_latest(self) -> None:
        bridge = GatewayBridge(request_timeout_seconds=10)
        websocket = FakeGatewayWebSocket()
        await websocket.accept()
        await bridge.attach(websocket)
        pending = await bridge.start_request(
            "a" * 64,
            {"input": "hello"},
            owner_client_id="c" * 64,
        )

        for text_value in ("draft", "replacement", "final snapshot"):
            await bridge.dispatch_event(
                {
                    "type": "assistant.snapshot",
                    "request_id": pending.request_id,
                    "session_id": pending.session_id,
                    "text": text_value,
                }
            )
        self.assertEqual(pending.queue.qsize(), 0)
        self.assertEqual(pending.latest_snapshot, "final snapshot")

        await bridge.dispatch_event(
            {
                "type": "request.completed",
                "request_id": pending.request_id,
                "session_id": pending.session_id,
            }
        )
        events = [event async for event in bridge.iter_events(pending)]

        self.assertEqual(events[0]["text"], "final snapshot")
        await bridge.release_request(pending, notify_gateway=False)

    def test_reasoning_text_is_bounded_by_utf8_bytes(self) -> None:
        accepted_text = "界" * (MAX_REASONING_TEXT_BYTES // 3)
        accepted = GatewayBridge._validate_event(
            {
                "type": "reasoning",
                "request_id": "request-1",
                "session_id": "a" * 64,
                "text": accepted_text,
            }
        )

        self.assertEqual(accepted["text"], accepted_text)
        with self.assertRaisesRegex(
            GatewayProtocolError,
            "Gateway reasoning event text exceeds the maximum size",
        ):
            GatewayBridge._validate_event(
                {
                    "type": "reasoning",
                    "request_id": "request-1",
                    "session_id": "a" * 64,
                    "text": accepted_text + "界",
                }
            )

    def test_assistant_snapshot_and_terminal_text_have_the_same_byte_limit(self) -> None:
        oversized_text = "x" * (MAX_ASSISTANT_TEXT_BYTES + 1)

        for event_type in ("assistant.snapshot", "request.completed"):
            with self.subTest(event_type=event_type), self.assertRaisesRegex(
                GatewayProtocolError,
                f"Gateway {event_type} event text exceeds the maximum size",
            ):
                GatewayBridge._validate_event(
                    {
                        "type": event_type,
                        "request_id": "request-1",
                        "session_id": "a" * 64,
                        "text": oversized_text,
                    }
                )

    def test_main_sets_explicit_websocket_frame_limit(self) -> None:
        with (
            patch.dict(os.environ, {"HOST": "127.0.0.1", "PORT": "50123"}),
            patch("uvicorn.run") as run,
        ):
            main.main()

        run.assert_called_once_with(
            "main:app",
            host="127.0.0.1",
            port=50123,
            reload=False,
            ws_max_size=MAX_GATEWAY_FRAME_BYTES,
        )

    async def test_event_queue_overload_fails_only_that_request_explicitly(self) -> None:
        bridge = GatewayBridge(request_timeout_seconds=10)
        websocket = FakeGatewayWebSocket()
        await websocket.accept()
        await bridge.attach(websocket)
        overloaded = await bridge.start_request(
            "a" * 64,
            {"input": "overloaded"},
            owner_client_id="c" * 64,
        )
        healthy = await bridge.start_request(
            "b" * 64,
            {"input": "healthy"},
            owner_client_id="d" * 64,
        )

        for index in range(overloaded.queue.maxsize + 1):
            await bridge.dispatch_event(
                {
                    "type": "reasoning",
                    "request_id": overloaded.request_id,
                    "session_id": overloaded.session_id,
                    "text": f"event-{index}",
                }
            )
        events = [event async for event in bridge.iter_events(overloaded)]

        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["type"], "request.failed")
        self.assertEqual(events[0]["error"], "Gateway event buffer exceeded its limit")
        self.assertFalse(healthy.terminal_received)
        self.assertEqual(healthy.queue.qsize(), 0)
        await bridge.release_request(overloaded, notify_gateway=True)
        await bridge.release_request(healthy, notify_gateway=False)

    async def test_start_frame_contains_only_internal_session_and_agent_payload(self) -> None:
        bridge = GatewayBridge(request_timeout_seconds=10)
        websocket = FakeGatewayWebSocket()
        await websocket.accept()
        self.assertTrue(await bridge.attach(websocket))

        pending = await bridge.start_request(
            session_id="f" * 64,
            payload={"input": "hello", "model": "hermes-agent"},
            owner_client_id="c" * 64,
        )

        self.assertEqual(len(websocket.sent), 1)
        frame = websocket.sent[0]
        self.assertEqual(frame["type"], "request.start")
        self.assertEqual(frame["session_id"], "f" * 64)
        self.assertEqual(frame["payload"], {"input": "hello", "model": "hermes-agent"})
        self.assertNotIn("client_id", json.dumps(frame))
        self.assertNotIn("conversation-1", json.dumps(frame))
        await bridge.release_request(pending, notify_gateway=False)

    async def test_same_session_rejects_second_inflight_request(self) -> None:
        bridge = GatewayBridge(request_timeout_seconds=10)
        websocket = FakeGatewayWebSocket()
        await websocket.accept()
        await bridge.attach(websocket)
        pending = await bridge.start_request(
            "f" * 64,
            {"input": "first"},
            owner_client_id="c" * 64,
        )

        with self.assertRaises(GatewaySessionBusyError):
            await bridge.start_request(
                "f" * 64,
                {"input": "second"},
                owner_client_id="c" * 64,
            )

        await bridge.release_request(pending, notify_gateway=False)

    async def test_only_one_gateway_connection_can_attach(self) -> None:
        bridge = GatewayBridge(request_timeout_seconds=10)

        first = FakeGatewayWebSocket()
        second = FakeGatewayWebSocket()
        await first.accept()
        await second.accept()
        self.assertTrue(await bridge.attach(first))
        self.assertFalse(await bridge.attach(second))

    async def test_start_without_gateway_connection_fails_explicitly(self) -> None:
        bridge = GatewayBridge(request_timeout_seconds=10)

        with self.assertRaises(GatewayUnavailableError) as raised:
            await bridge.start_request(
                "a" * 64,
                {"input": "hello"},
                owner_client_id="c" * 64,
            )

        self.assertEqual(str(raised.exception), "Hermes Gateway bridge is not connected")

    async def test_events_are_routed_by_request_and_session(self) -> None:
        bridge = GatewayBridge(request_timeout_seconds=10)
        websocket = FakeGatewayWebSocket()
        await websocket.accept()
        await bridge.attach(websocket)
        first = await bridge.start_request(
            "a" * 64,
            {"input": "first"},
            owner_client_id="c" * 64,
        )
        second = await bridge.start_request(
            "b" * 64,
            {"input": "second"},
            owner_client_id="d" * 64,
        )

        await bridge.dispatch_event(
            {
                "type": "assistant.snapshot",
                "request_id": second.request_id,
                "session_id": second.session_id,
                "text": "second answer",
            }
        )
        await bridge.dispatch_event(
            {
                "type": "request.completed",
                "request_id": second.request_id,
                "session_id": second.session_id,
                "text": "second answer",
            }
        )

        events = [event async for event in bridge.iter_events(second)]

        self.assertEqual([event["type"] for event in events], ["request.completed"])
        self.assertEqual(events[0]["text"], "second answer")
        self.assertEqual(first.queue.qsize(), 0)
        await bridge.release_request(first, notify_gateway=False)
        await bridge.release_request(second, notify_gateway=False)

    async def test_mismatched_session_event_is_rejected_as_protocol_error(self) -> None:
        bridge = GatewayBridge(request_timeout_seconds=10)
        websocket = FakeGatewayWebSocket()
        await websocket.accept()
        await bridge.attach(websocket)
        pending = await bridge.start_request(
            "a" * 64,
            {"input": "hello"},
            owner_client_id="c" * 64,
        )

        with self.assertRaises(GatewayProtocolError) as raised:
            await bridge.dispatch_event(
                {
                    "type": "assistant.snapshot",
                    "request_id": pending.request_id,
                    "session_id": "b" * 64,
                    "text": "wrong session",
                }
            )

        self.assertEqual(
            str(raised.exception),
            "Gateway event session_id does not match request",
        )
        self.assertEqual(pending.queue.qsize(), 0)
        await bridge.release_request(pending, notify_gateway=False)

    async def test_disconnect_fails_pending_request_explicitly(self) -> None:
        bridge = GatewayBridge(request_timeout_seconds=10)
        websocket = FakeGatewayWebSocket()
        await websocket.accept()
        await bridge.attach(websocket)
        pending = await bridge.start_request(
            "a" * 64,
            {"input": "hello"},
            owner_client_id="c" * 64,
        )
        await bridge.dispatch_event(
            {
                "type": "clarify.request",
                "request_id": pending.request_id,
                "session_id": pending.session_id,
                "clarify_id": "clarify-" + "6" * 24,
                "question": "是否继续？",
                "choices": ["继续", "取消"],
            }
        )

        await bridge.detach(websocket, "Gateway bridge disconnected")
        events = [event async for event in bridge.iter_events(pending)]

        self.assertEqual(events, [
            {
                "type": "request.failed",
                "request_id": pending.request_id,
                "session_id": pending.session_id,
                "error": "Gateway bridge disconnected",
            }
        ])
        self.assertIsNone(pending.current_clarification)
        self.assertFalse(bridge.connected)


class GatewayRequestRouteTests(unittest.IsolatedAsyncioTestCase):
    async def test_approval_route_waits_for_ack_and_maps_owner_and_duplicate(self) -> None:
        bridge = GatewayBridge(request_timeout_seconds=10)
        websocket = FakeGatewayWebSocket()
        await websocket.accept()
        await bridge.attach(websocket)
        owner_client_id = "c" * 64
        pending = await bridge.start_request(
            "a" * 64,
            {"input": "hello"},
            owner_client_id=owner_client_id,
        )
        approval_id = "approval-route-1"
        await bridge.dispatch_event(
            {
                "type": "approval.request",
                "request_id": pending.request_id,
                "session_id": pending.session_id,
                "approval_id": approval_id,
                "command": "systemctl restart i2stream",
                "description": "重启服务",
                "allow_permanent": True,
                "allow_session": True,
                "smart_denied": False,
            }
        )

        with patch.object(main, "gateway_bridge", bridge):
            with self.assertRaises(HTTPException) as missing:
                await main.respond_to_approval(
                    pending.request_id,
                    approval_id,
                    main.ApprovalResponse(choice="once"),
                    "d" * 64,
                    None,
                )
            response_task = asyncio.create_task(
                main.respond_to_approval(
                    pending.request_id,
                    approval_id,
                    main.ApprovalResponse(choice="always"),
                    owner_client_id,
                    None,
                )
            )
            for _ in range(100):
                if websocket.sent[-1]["type"] == "approval.respond":
                    break
                await asyncio.sleep(0)
            else:
                self.fail("approval.respond frame was not sent")
            self.assertFalse(response_task.done())
            await bridge.dispatch_event(
                {
                    "type": "approval.response",
                    "request_id": pending.request_id,
                    "session_id": pending.session_id,
                    "approval_id": approval_id,
                    "accepted": True,
                }
            )
            result = await response_task
            with self.assertRaises(HTTPException) as duplicate:
                await main.respond_to_approval(
                    pending.request_id,
                    approval_id,
                    main.ApprovalResponse(choice="deny"),
                    owner_client_id,
                    None,
                )

        self.assertEqual(missing.exception.status_code, 404)
        self.assertEqual(duplicate.exception.status_code, 409)
        self.assertEqual(
            result,
            {
                "code": 0,
                "status": "success",
                "request_id": pending.request_id,
                "approval_id": approval_id,
            },
        )
        await bridge.release_request(pending, notify_gateway=False)

    async def test_clarification_route_enforces_owner_and_maps_missing_and_duplicate(self) -> None:
        bridge = GatewayBridge(request_timeout_seconds=10)
        websocket = FakeGatewayWebSocket()
        await websocket.accept()
        await bridge.attach(websocket)
        owner_client_id = "c" * 64
        pending = await bridge.start_request(
            "a" * 64,
            {"input": "hello"},
            owner_client_id=owner_client_id,
        )
        clarify_id = "clarify-4"
        await bridge.dispatch_event(
            {
                "type": "clarify.request",
                "request_id": pending.request_id,
                "session_id": pending.session_id,
                "clarify_id": clarify_id,
                "question": "是否继续？",
                "choices": ["继续", "取消"],
            }
        )

        with patch.object(main, "gateway_bridge", bridge):
            with self.assertRaises(HTTPException) as missing:
                await main.respond_to_clarification(
                    pending.request_id,
                    clarify_id,
                    main.ClarificationResponse(response="继续"),
                    "d" * 64,
                    None,
                )
            response_task = asyncio.create_task(
                main.respond_to_clarification(
                    pending.request_id,
                    clarify_id,
                    main.ClarificationResponse(response=" 继续 "),
                    owner_client_id,
                    None,
                )
            )
            for _ in range(100):
                if websocket.sent[-1]["type"] == "clarify.respond":
                    break
                await asyncio.sleep(0)
            else:
                self.fail("clarify.respond frame was not sent")
            await bridge.dispatch_event(
                {
                    "type": "clarify.response",
                    "request_id": pending.request_id,
                    "session_id": pending.session_id,
                    "clarify_id": clarify_id,
                    "accepted": True,
                }
            )
            result = await response_task
            with self.assertRaises(HTTPException) as duplicate:
                await main.respond_to_clarification(
                    pending.request_id,
                    clarify_id,
                    main.ClarificationResponse(response="取消"),
                    owner_client_id,
                    None,
                )

        self.assertEqual(missing.exception.status_code, 404)
        self.assertEqual(result, {
            "code": 0,
            "status": "success",
            "request_id": pending.request_id,
            "clarification_id": clarify_id,
        })
        self.assertEqual(duplicate.exception.status_code, 409)
        self.assertEqual(websocket.sent[-1]["response"], "继续")
        await bridge.release_request(pending, notify_gateway=False)

    async def test_clarification_route_maps_rejected_ack_to_conflict(self) -> None:
        bridge = GatewayBridge(request_timeout_seconds=10)
        websocket = FakeGatewayWebSocket()
        await websocket.accept()
        await bridge.attach(websocket)
        owner_client_id = "c" * 64
        pending = await bridge.start_request(
            "a" * 64,
            {"input": "hello"},
            owner_client_id=owner_client_id,
        )
        clarify_id = "clarify-route-rejected"
        await bridge.dispatch_event(
            {
                "type": "clarify.request",
                "request_id": pending.request_id,
                "session_id": pending.session_id,
                "clarify_id": clarify_id,
                "question": "是否继续？",
                "choices": [],
            }
        )

        with patch.object(main, "gateway_bridge", bridge):
            response_task = asyncio.create_task(
                main.respond_to_clarification(
                    pending.request_id,
                    clarify_id,
                    main.ClarificationResponse(response="继续"),
                    owner_client_id,
                    None,
                )
            )
            for _ in range(100):
                if websocket.sent[-1]["type"] == "clarify.respond":
                    break
                await asyncio.sleep(0)
            else:
                self.fail("clarify.respond frame was not sent")
            await bridge.dispatch_event(
                {
                    "type": "clarify.response",
                    "request_id": pending.request_id,
                    "session_id": pending.session_id,
                    "clarify_id": clarify_id,
                    "accepted": False,
                }
            )

            with self.assertRaises(HTTPException) as rejected:
                await response_task

        self.assertEqual(rejected.exception.status_code, 409)
        await bridge.release_request(pending, notify_gateway=False)

    async def test_clarification_route_rejects_blank_and_oversized_response(self) -> None:
        with self.assertRaises(HTTPException) as blank:
            await main.respond_to_clarification(
                "r" * 32,
                "c" * 32,
                main.ClarificationResponse(response="  "),
                "a" * 64,
                None,
            )
        with self.assertRaises(ValidationError):
            main.ClarificationResponse(response="x" * 10_001)

        self.assertEqual(blank.exception.status_code, 400)

    async def test_user_history_failure_happens_before_gateway_request_is_started(self) -> None:
        bridge = GatewayBridge(request_timeout_seconds=10)
        websocket = FakeGatewayWebSocket()
        await websocket.accept()
        await bridge.attach(websocket)
        request = FakeRequest(
            {"input": "hello", "conversation": "conversation-1"},
            {"X-I2H-Client-Id": "a" * 64},
        )

        with (
            patch.object(main, "gateway_bridge", bridge),
            patch.object(main, "insert_chat_message", side_effect=RuntimeError("sqlite failed")),
        ):
            with self.assertRaisesRegex(RuntimeError, "sqlite failed"):
                await main.responses_proxy(request, None)

        self.assertEqual(websocket.sent, [])

    async def test_response_construction_failure_releases_gateway_session(self) -> None:
        bridge = GatewayBridge(request_timeout_seconds=10)
        websocket = FakeGatewayWebSocket()
        await websocket.accept()
        await bridge.attach(websocket)
        client_id = "a" * 64
        conversation_id = "conversation-1"
        request = FakeRequest(
            {"input": "hello", "conversation": conversation_id},
            {"X-I2H-Client-Id": client_id},
        )

        with (
            patch.object(main, "gateway_bridge", bridge),
            patch.object(main, "insert_chat_message"),
            patch.object(
                main,
                "GatewayStreamingResponse",
                side_effect=RuntimeError("response construction failed"),
            ),
        ):
            with self.assertRaisesRegex(RuntimeError, "response construction failed"):
                await main.responses_proxy(request, None)

        session_id = derive_gateway_session_id(
            client_id,
            conversation_id,
            main.settings.session_hmac_secret,
        )
        retry = await bridge.start_request(
            session_id,
            {"input": "retry"},
            owner_client_id=client_id,
        )
        await bridge.release_request(retry, notify_gateway=False)

    async def test_websocket_rejects_missing_bearer_token_before_accept(self) -> None:
        websocket = FakeGatewayWebSocket()

        await main.internal_gateway(websocket)

        self.assertFalse(websocket.accepted)
        self.assertEqual(websocket.closed, (1008, "Invalid gateway authorization"))

    async def test_authorized_websocket_is_accepted_and_detached_on_disconnect(self) -> None:
        websocket = FakeGatewayWebSocket()
        websocket.headers["authorization"] = (
            f"Bearer {main.settings.gateway_bridge_token.get_secret_value()}"
        )
        bridge = GatewayBridge(request_timeout_seconds=10)

        with patch.object(main, "gateway_bridge", bridge):
            await main.internal_gateway(websocket)

        self.assertTrue(websocket.accepted)
        self.assertIsNone(websocket.closed)
        self.assertFalse(bridge.connected)

    async def test_duplicate_websocket_is_accepted_before_policy_close(self) -> None:
        first = FakeGatewayWebSocket()
        await first.accept()
        bridge = GatewayBridge(request_timeout_seconds=10)
        await bridge.attach(first)
        duplicate = FakeGatewayWebSocket()
        duplicate.headers["authorization"] = (
            f"Bearer {main.settings.gateway_bridge_token.get_secret_value()}"
        )

        with patch.object(main, "gateway_bridge", bridge):
            await main.internal_gateway(duplicate)

        self.assertTrue(duplicate.accepted)
        self.assertEqual(
            duplicate.closed,
            (1008, "A gateway bridge is already connected"),
        )
        self.assertTrue(bridge.connected)

    async def test_route_removes_public_conversation_before_starting_gateway_request(self) -> None:
        captured: dict[str, object] = {}

        class FakeBridge:
            async def start_request(self, session_id, payload, owner_client_id):
                captured["session_id"] = session_id
                captured["payload"] = payload
                captured["owner_client_id"] = owner_client_id
                raise GatewaySessionBusyError("busy")

        request = FakeRequest(
            {
                "model": "hermes-agent",
                "input": "hello",
                "conversation": "conversation-1",
                "store": True,
                "stream": True,
            },
            {"X-I2H-Client-Id": "a" * 64},
        )

        original_bridge = main.gateway_bridge
        main.gateway_bridge = FakeBridge()
        try:
            with (
                patch.object(main, "insert_chat_message") as inserted_message,
                self.assertRaises(HTTPException) as raised,
            ):
                await main.responses_proxy(request, None)
        finally:
            main.gateway_bridge = original_bridge

        self.assertEqual(raised.exception.status_code, 409)
        self.assertEqual(
            captured["session_id"],
            derive_gateway_session_id("a" * 64, "conversation-1", main.settings.session_hmac_secret),
        )
        self.assertEqual(
            captured["payload"],
            {"model": "hermes-agent", "input": "hello", "store": True, "stream": True},
        )
        self.assertEqual(captured["owner_client_id"], "a" * 64)
        inserted_message.assert_called_once_with(
            "a" * 64,
            "conversation-1",
            "user",
            "hello",
            None,
        )

    def test_internal_gateway_route_is_registered(self) -> None:
        paths = {route.path for route in main.app.routes}

        self.assertIn("/internal/gateway", paths)
        self.assertIn(
            "/api/agent/requests/{request_id}/clarifications/{clarify_id}",
            paths,
        )
        self.assertIn(
            "/api/agent/requests/{request_id}/approvals/{approval_id}",
            paths,
        )

    async def test_unavailable_gateway_returns_503_instead_of_http_fallback(self) -> None:
        request = FakeRequest(
            {"input": "hello", "conversation": "conversation-1"},
            {"X-I2H-Client-Id": "a" * 64},
        )
        bridge = GatewayBridge(request_timeout_seconds=10)

        with (
            patch.object(main, "gateway_bridge", bridge),
            patch.object(main, "insert_chat_message"),
        ):
            with self.assertRaises(HTTPException) as raised:
                await main.responses_proxy(request, None)

        self.assertEqual(raised.exception.status_code, 503)
        self.assertEqual(raised.exception.detail, "Hermes Gateway bridge is not connected")

    async def test_health_reports_gateway_connection_without_secret_values(self) -> None:
        bridge = GatewayBridge(request_timeout_seconds=10)
        websocket = FakeGatewayWebSocket()
        await websocket.accept()

        with patch.object(main, "gateway_bridge", bridge):
            disconnected = await main.health()
            await bridge.attach(websocket)
            connected = await main.health()

        self.assertFalse(disconnected["gateway_bridge_connected"])
        self.assertTrue(connected["gateway_bridge_connected"])
        serialized = json.dumps(connected)
        self.assertNotIn(main.settings.session_hmac_secret.get_secret_value(), serialized)
        self.assertNotIn(main.settings.gateway_bridge_token.get_secret_value(), serialized)


class GatewaySseStreamTests(unittest.IsolatedAsyncioTestCase):
    async def test_progress_and_interaction_cards_keep_gateway_event_order(self) -> None:
        bridge = GatewayBridge(request_timeout_seconds=10)
        websocket = FakeGatewayWebSocket()
        await websocket.accept()
        await bridge.attach(websocket)
        pending = await bridge.start_request(
            "a" * 64,
            {"input": "hello"},
            owner_client_id="c" * 64,
        )
        clarify_id = "clarify-" + "6" * 24
        for event in (
            {
                "type": "reasoning",
                "request_id": pending.request_id,
                "session_id": pending.session_id,
                "text": "授权前分析",
            },
            {
                "type": "approval.request",
                "request_id": pending.request_id,
                "session_id": pending.session_id,
                "approval_id": "approval-order-1",
                "command": "systemctl restart example",
                "description": "重启服务",
                "allow_permanent": False,
                "allow_session": True,
                "smart_denied": False,
            },
            {
                "type": "reasoning",
                "request_id": pending.request_id,
                "session_id": pending.session_id,
                "text": "授权后分析",
            },
            {
                "type": "clarify.request",
                "request_id": pending.request_id,
                "session_id": pending.session_id,
                "clarify_id": clarify_id,
                "question": "选择处理范围",
                "choices": ["当前节点", "全部节点"],
            },
            {
                "type": "reasoning",
                "request_id": pending.request_id,
                "session_id": pending.session_id,
                "text": "确认后分析",
            },
            {
                "type": "request.completed",
                "request_id": pending.request_id,
                "session_id": pending.session_id,
                "text": "处理完成",
            },
        ):
            await bridge.dispatch_event(event)

        with (
            patch.object(main, "gateway_bridge", bridge),
            patch.object(main, "insert_chat_message", Mock(return_value=1783409846000003)),
            patch.object(
                main,
                "append_progress",
                Mock(
                    side_effect=[
                        {"seq": 2, "message": "授权前分析", "created_at": 1.0},
                        {"seq": 3, "message": "授权后分析", "created_at": 2.0},
                        {"seq": 4, "message": "确认后分析", "created_at": 3.0},
                    ]
                ),
            ),
            patch.object(main, "finish_progress"),
        ):
            chunks = [
                chunk
                async for chunk in main.persisted_gateway_stream(
                    pending,
                    "c" * 64,
                    "conversation-1",
                    "progress-request-1",
                )
            ]

        events = [
            event
            for chunk in chunks
            if (event := parse_sse_data(chunk.decode("utf-8"))) is not None
        ]
        timeline = []
        for event in events:
            if event["type"] == "proxy.progress":
                timeline.append(("reasoning", event["message"], event["seq"]))
            elif event["type"] == "response.approval.requested":
                timeline.append(("approval", event["approval_id"]))
            elif event["type"] == "response.clarification.requested":
                timeline.append(("clarify", event["clarification_id"]))

        self.assertEqual(
            timeline,
            [
                ("reasoning", "授权前分析", 2),
                ("approval", "approval-order-1"),
                ("reasoning", "授权后分析", 3),
                ("clarify", clarify_id),
                ("reasoning", "确认后分析", 4),
            ],
        )

    async def test_approval_request_is_emitted_as_browser_event_without_chat_history(self) -> None:
        bridge = GatewayBridge(request_timeout_seconds=10)
        websocket = FakeGatewayWebSocket()
        await websocket.accept()
        await bridge.attach(websocket)
        pending = await bridge.start_request(
            "a" * 64,
            {"input": "hello"},
            owner_client_id="c" * 64,
        )
        await bridge.dispatch_event(
            {
                "type": "approval.request",
                "request_id": pending.request_id,
                "session_id": pending.session_id,
                "approval_id": "approval-sse-1",
                "command": "rm -f /tmp/example",
                "description": "删除临时文件",
                "allow_permanent": False,
                "allow_session": True,
                "smart_denied": False,
            }
        )
        inserted_message = Mock()

        with (
            patch.object(main, "gateway_bridge", bridge),
            patch.object(main, "insert_chat_message", inserted_message),
            patch.object(main, "finish_progress"),
        ):
            stream = main.persisted_gateway_stream(
                pending,
                "c" * 64,
                "conversation-1",
            )
            created = parse_sse_data((await anext(stream)).decode("utf-8"))
            approval = parse_sse_data((await anext(stream)).decode("utf-8"))
            await stream.aclose()

        self.assertEqual(created, {"type": "response.created"})
        self.assertEqual(
            approval,
            {
                "type": "response.approval.requested",
                "request_id": pending.request_id,
                "approval_id": "approval-sse-1",
                "command": "rm -f /tmp/example",
                "description": "删除临时文件",
                "allow_permanent": False,
                "allow_session": True,
                "smart_denied": False,
            },
        )
        inserted_message.assert_not_called()

    async def test_clarify_request_is_emitted_as_browser_event_without_chat_history(self) -> None:
        bridge = GatewayBridge(request_timeout_seconds=10)
        websocket = FakeGatewayWebSocket()
        await websocket.accept()
        await bridge.attach(websocket)
        pending = await bridge.start_request(
            "a" * 64,
            {"input": "hello"},
            owner_client_id="c" * 64,
        )
        clarify_id = "clarify-" + "5" * 24
        await bridge.dispatch_event(
            {
                "type": "clarify.request",
                "request_id": pending.request_id,
                "session_id": pending.session_id,
                "clarify_id": clarify_id,
                "question": "选择授权范围",
                "choices": ["本次允许", "拒绝"],
            }
        )
        inserted_message = Mock()

        with (
            patch.object(main, "gateway_bridge", bridge),
            patch.object(main, "insert_chat_message", inserted_message),
            patch.object(main, "finish_progress"),
        ):
            stream = main.persisted_gateway_stream(
                pending,
                "c" * 64,
                "conversation-1",
            )
            created = parse_sse_data((await anext(stream)).decode("utf-8"))
            clarification = parse_sse_data((await anext(stream)).decode("utf-8"))
            await stream.aclose()

        self.assertEqual(created, {"type": "response.created"})
        self.assertEqual(
            clarification,
            {
                "type": "response.clarification.requested",
                "request_id": pending.request_id,
                "clarification_id": clarify_id,
                "question": "选择授权范围",
                "choices": ["本次允许", "拒绝"],
            },
        )
        inserted_message.assert_not_called()

    async def test_protocol_error_is_logged_but_only_generic_failure_is_returned(self) -> None:
        pending = PendingGatewayRequest(
            request_id="request-id-123456",
            session_id="a" * 64,
            owner_client_id="c" * 64,
        )

        class ProtocolFailureBridge:
            async def iter_events(self, _pending):
                raise GatewayProtocolError("sensitive protocol detail")
                yield {}

            async def release_request(self, _pending, *, notify_gateway):
                return None

        inserted_message = Mock()
        with (
            patch.object(main, "gateway_bridge", ProtocolFailureBridge()),
            patch.object(main, "insert_chat_message", inserted_message),
            patch.object(main, "finish_progress"),
            self.assertLogs(main.__name__, level="ERROR") as captured_logs,
        ):
            chunks = [
                chunk
                async for chunk in main.persisted_gateway_stream(
                    pending,
                    "client-id",
                    "conversation-1",
                )
            ]

        response_text = b"".join(chunks).decode("utf-8")
        events = [
            event
            for chunk in chunks
            if (event := parse_sse_data(chunk.decode("utf-8"))) is not None
        ]
        self.assertEqual(
            events[-1],
            {
                "type": "response.failed",
                "error": {"message": "Agent request failed. Please retry."},
            },
        )
        self.assertNotIn("sensitive protocol detail", response_text)
        self.assertTrue(response_text.endswith("data: [DONE]\n\n"))
        self.assertIn("sensitive protocol detail", "\n".join(captured_logs.output))
        inserted_message.assert_not_called()

    async def test_response_lifecycle_releases_session_when_body_fails_before_first_chunk(self) -> None:
        bridge = GatewayBridge(request_timeout_seconds=10)
        websocket = FakeGatewayWebSocket()
        await websocket.accept()
        await bridge.attach(websocket)
        pending = await bridge.start_request(
            "a" * 64,
            {"input": "hello"},
            owner_client_id="c" * 64,
        )

        async def fail_before_first_chunk():
            raise RuntimeError("pre-stream failure")
            yield b""

        response = main.GatewayStreamingResponse(
            fail_before_first_chunk(),
            pending=pending,
            bridge=bridge,
            progress_client_id="client-id",
            progress_request_id=None,
        )

        async def receive():
            return {"type": "http.disconnect"}

        async def send(_message):
            return None

        scope = {
            "type": "http",
            "asgi": {"version": "3.0", "spec_version": "2.4"},
            "http_version": "1.1",
            "method": "POST",
            "scheme": "http",
            "path": "/api/agent/requests",
            "raw_path": b"/api/agent/requests",
            "query_string": b"",
            "headers": [],
            "client": ("127.0.0.1", 12345),
            "server": ("127.0.0.1", 50091),
        }
        with self.assertRaisesRegex(RuntimeError, "pre-stream failure"):
            await response(scope, receive, send)

        retry = await bridge.start_request(
            "a" * 64,
            {"input": "retry"},
            owner_client_id="c" * 64,
        )
        self.assertEqual(websocket.sent[-2]["type"], "request.cancel")
        self.assertEqual(websocket.sent[-1]["type"], "request.start")
        await bridge.release_request(retry, notify_gateway=False)

    async def test_unstarted_response_cleanup_releases_session_for_retry(self) -> None:
        bridge = GatewayBridge(request_timeout_seconds=10)
        websocket = FakeGatewayWebSocket()
        await websocket.accept()
        await bridge.attach(websocket)
        pending = await bridge.start_request(
            "a" * 64,
            {"input": "hello"},
            owner_client_id="c" * 64,
        )

        async def never_started():
            raise AssertionError("body iterator must not start")
            yield b""

        response = main.GatewayStreamingResponse(
            never_started(),
            pending=pending,
            bridge=bridge,
            progress_client_id="client-id",
            progress_request_id=None,
        )
        await response.cleanup()

        retry = await bridge.start_request(
            "a" * 64,
            {"input": "retry"},
            owner_client_id="c" * 64,
        )
        self.assertEqual(websocket.sent[-2]["type"], "request.cancel")
        self.assertEqual(websocket.sent[-1]["type"], "request.start")
        await bridge.release_request(retry, notify_gateway=False)

    async def test_closing_browser_stream_sends_request_cancel(self) -> None:
        bridge = GatewayBridge(request_timeout_seconds=10)
        websocket = FakeGatewayWebSocket()
        await websocket.accept()
        await bridge.attach(websocket)
        pending = await bridge.start_request(
            "a" * 64,
            {"input": "hello"},
            owner_client_id="c" * 64,
        )

        with (
            patch.object(main, "gateway_bridge", bridge),
            patch.object(main, "insert_chat_message"),
            patch.object(main, "finish_progress"),
        ):
            stream = main.persisted_gateway_stream(
                pending,
                "client-id",
                "conversation-1",
            )
            first_chunk = await anext(stream)
            await stream.aclose()

        self.assertEqual(
            parse_sse_data(first_chunk.decode("utf-8")),
            {"type": "response.created"},
        )
        self.assertEqual(
            websocket.sent[-1],
            {
                "type": "request.cancel",
                "request_id": pending.request_id,
                "session_id": pending.session_id,
            },
        )

    async def test_snapshot_replacements_emit_only_final_text_and_keep_live_progress(self) -> None:
        bridge = GatewayBridge(request_timeout_seconds=10)
        websocket = FakeGatewayWebSocket()
        await websocket.accept()
        await bridge.attach(websocket)
        pending = await bridge.start_request(
            "a" * 64,
            {"input": "hello"},
            owner_client_id="c" * 64,
        )
        for event in (
            {
                "type": "reasoning",
                "request_id": pending.request_id,
                "session_id": pending.session_id,
                "text": "正在分析输入。",
            },
            {
                "type": "tool.started",
                "request_id": pending.request_id,
                "session_id": pending.session_id,
                "name": "terminal",
            },
            {
                "type": "assistant.snapshot",
                "request_id": pending.request_id,
                "session_id": pending.session_id,
                "text": "Draft answer that will be replaced",
            },
            {
                "type": "assistant.snapshot",
                "request_id": pending.request_id,
                "session_id": pending.session_id,
                "text": "Final answer",
            },
            {
                "type": "tool.completed",
                "request_id": pending.request_id,
                "session_id": pending.session_id,
                "name": "terminal",
            },
            {
                "type": "request.completed",
                "request_id": pending.request_id,
                "session_id": pending.session_id,
                "text": "Final answer!",
            },
        ):
            await bridge.dispatch_event(event)

        inserted_message = Mock(return_value=1783409846000001)
        progress_message = Mock(return_value=None)
        finish = Mock()
        with (
            patch.object(main, "gateway_bridge", bridge),
            patch.object(main, "insert_chat_message", inserted_message),
            patch.object(main, "append_progress", progress_message),
            patch.object(main, "finish_progress", finish),
        ):
            chunks = [
                chunk
                async for chunk in main.persisted_gateway_stream(
                    pending,
                    "client-id",
                    "conversation-1",
                    "request-id-123456",
                )
            ]

        parsed_events = [
            event
            for chunk in chunks
            if (event := parse_sse_data(chunk.decode("utf-8"))) is not None
        ]
        deltas = [
            event["delta"]
            for event in parsed_events
            if event["type"] == "response.output_text.delta"
        ]
        self.assertEqual(deltas, ["Final answer!"])
        persisted_index = next(
            index
            for index, event in enumerate(parsed_events)
            if event["type"] == "proxy.message.persisted"
        )
        completed_index = next(
            index
            for index, event in enumerate(parsed_events)
            if event["type"] == "response.completed"
        )
        self.assertLess(persisted_index, completed_index)
        self.assertEqual(
            parsed_events[persisted_index],
            {
                "type": "proxy.message.persisted",
                "role": "assistant",
                "message_id": 1783409846000001,
            },
        )
        self.assertEqual(
            [call.args for call in progress_message.call_args_list],
            [
                ("client-id", "request-id-123456", "正在分析输入。"),
                ("client-id", "request-id-123456", "正在调用工具：terminal"),
                ("client-id", "request-id-123456", "已完成调用工具：terminal"),
            ],
        )
        self.assertEqual(
            [call.args for call in inserted_message.call_args_list],
            [
                ("client-id", "conversation-1", "assistant", "Final answer!", None),
            ],
        )
        finish.assert_called_once_with("client-id", "request-id-123456")

    async def test_completed_media_is_registered_cleaned_and_saved_with_history(self) -> None:
        bridge = GatewayBridge(request_timeout_seconds=10)
        websocket = FakeGatewayWebSocket()
        await websocket.accept()
        await bridge.attach(websocket)
        pending = await bridge.start_request(
            "a" * 64,
            {"input": "hello"},
            owner_client_id="c" * 64,
        )
        inserted_message = Mock(return_value=1783409846000002)
        file_records.clear()

        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            source_path = temp_path / "hello.html"
            source_path.write_text("<html>Hello</html>", encoding="utf-8")
            store_path = temp_path / "store"
            await bridge.dispatch_event(
                {
                    "type": "request.completed",
                    "request_id": pending.request_id,
                    "session_id": pending.session_id,
                    "text": f"Created file.\nMEDIA:{source_path}",
                }
            )

            with (
                patch.object(main, "gateway_bridge", bridge),
                patch.object(main, "insert_chat_message", inserted_message),
                patch.object(main.settings, "file_store_dir", store_path),
            ):
                chunks = [
                    chunk
                    async for chunk in main.persisted_gateway_stream(
                        pending,
                        "client-id",
                        "conversation-1",
                    )
                ]

        events = [
            event
            for chunk in chunks
            if (event := parse_sse_data(chunk.decode("utf-8"))) is not None
        ]
        delta_event = next(
            event for event in events if event["type"] == "response.output_text.delta"
        )
        file_event = next(event for event in events if event["type"] == "proxy.file")
        self.assertEqual(delta_event["delta"], "Created file.\n")
        self.assertNotIn("MEDIA:", json.dumps(events))
        self.assertEqual(file_event["file"]["name"], "hello.html")
        inserted_message.assert_called_once_with(
            "client-id",
            "conversation-1",
            "assistant",
            "Created file.\n",
            {"files": [file_event["file"]]},
        )
        file_records.clear()

    async def test_bridge_disconnect_becomes_visible_sse_failure(self) -> None:
        bridge = GatewayBridge(request_timeout_seconds=10)
        websocket = FakeGatewayWebSocket()
        await websocket.accept()
        await bridge.attach(websocket)
        pending = await bridge.start_request(
            "a" * 64,
            {"input": "hello"},
            owner_client_id="c" * 64,
        )
        await bridge.detach(websocket, "Gateway bridge disconnected")

        inserted_message = Mock()
        with (
            patch.object(main, "gateway_bridge", bridge),
            patch.object(main, "insert_chat_message", inserted_message),
            patch.object(main, "finish_progress"),
            self.assertLogs(main.__name__, level="ERROR") as captured_logs,
        ):
            chunks = [
                chunk
                async for chunk in main.persisted_gateway_stream(
                    pending,
                    "client-id",
                    "conversation-1",
                )
            ]

        events = [
            event
            for chunk in chunks
            if (event := parse_sse_data(chunk.decode("utf-8"))) is not None
        ]
        self.assertEqual(
            events[-1],
            {
                "type": "response.failed",
                "error": {"message": "Agent request failed. Please retry."},
            },
        )
        response_text = b"".join(chunks).decode("utf-8")
        self.assertNotIn("Gateway bridge disconnected", response_text)
        self.assertTrue(response_text.endswith("data: [DONE]\n\n"))
        self.assertIn("Gateway bridge disconnected", "\n".join(captured_logs.output))
        inserted_message.assert_not_called()


if __name__ == "__main__":
    unittest.main()
