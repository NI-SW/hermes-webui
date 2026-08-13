from __future__ import annotations

import asyncio
import json
import unittest

import httpx
from pydantic import SecretStr

from dashboard_agent import (
    DashboardAgentError,
    DashboardAgentService,
    DashboardAgentSessionBusyError,
)


class MemoryDashboardSessionStore:
    def __init__(self) -> None:
        self.owners: dict[str, str] = {}

    def add(self, owner_id: str, session_id: str) -> None:
        if session_id in self.owners:
            raise ValueError(f"duplicate session: {session_id}")
        self.owners[session_id] = owner_id

    def list_session_ids(self, owner_id: str) -> list[str]:
        return [
            session_id
            for session_id, stored_owner_id in reversed(self.owners.items())
            if stored_owner_id == owner_id
        ]

    def owns(self, owner_id: str, session_id: str) -> bool:
        return self.owners.get(session_id) == owner_id

    def remove(self, owner_id: str, session_id: str) -> bool:
        if not self.owns(owner_id, session_id):
            return False
        del self.owners[session_id]
        return True


class DashboardAgentServiceTests(unittest.IsolatedAsyncioTestCase):
    owner_id = "a" * 64
    other_owner_id = "b" * 64

    async def asyncTearDown(self) -> None:
        service = getattr(self, "service", None)
        if service is not None:
            await service.stop()
        client = getattr(self, "client", None)
        if client is not None:
            await client.aclose()

    async def build_service(self, handler) -> DashboardAgentService:
        self.store = MemoryDashboardSessionStore()
        self.client = httpx.AsyncClient(
            base_url="http://hermes.test:8641",
            transport=httpx.MockTransport(handler),
        )
        self.service = DashboardAgentService(
            base_url="http://hermes.test:8641",
            api_key=SecretStr("test-stream-qa-key"),
            request_timeout_seconds=5.0,
            session_store=self.store,
            client=self.client,
        )
        await self.service.start()
        return self.service

    async def wait_for_done(self, run_id: str) -> dict:
        for _ in range(100):
            snapshot = await self.service.get_run_events(self.owner_id, run_id, after=0)
            if snapshot["done"]:
                return snapshot
            await asyncio.sleep(0)
        self.fail(f"run {run_id} did not finish")

    async def test_list_sessions_only_fetches_sessions_owned_by_user(self) -> None:
        async def handler(request: httpx.Request) -> httpx.Response:
            self.assertEqual(request.headers["authorization"], "Bearer test-stream-qa-key")
            self.assertEqual(request.url.path, "/api/sessions/api_session_1")
            return httpx.Response(
                200,
                json={
                    "object": "hermes.session",
                    "session": {"id": "api_session_1", "title": "同步问题", "message_count": 2},
                },
            )

        service = await self.build_service(handler)
        self.store.add(self.owner_id, "api_session_1")
        self.store.add(self.other_owner_id, "api_session_2")

        sessions = await service.list_sessions(self.owner_id)

        self.assertEqual(
            sessions,
            [{"id": "api_session_1", "title": "同步问题", "message_count": 2}],
        )

    async def test_session_owned_by_another_user_is_rejected_before_upstream_access(self) -> None:
        async def handler(request: httpx.Request) -> httpx.Response:
            raise AssertionError(f"unexpected request: {request.method} {request.url}")

        service = await self.build_service(handler)
        self.store.add(self.owner_id, "api_private")

        with self.assertRaisesRegex(DashboardAgentError, "not found") as raised:
            await service.list_messages(self.other_owner_id, "api_private")

        self.assertEqual(raised.exception.status_code, 404)

    async def test_create_and_delete_session_update_persistent_ownership(self) -> None:
        async def handler(request: httpx.Request) -> httpx.Response:
            if request.method == "POST" and request.url.path == "/api/sessions":
                return httpx.Response(
                    201,
                    json={
                        "object": "hermes.session",
                        "session": {"id": "api_created", "title": "新对话"},
                    },
                )
            if request.method == "DELETE" and request.url.path == "/api/sessions/api_created":
                return httpx.Response(200, json={"deleted": True})
            raise AssertionError(f"unexpected request: {request.method} {request.url}")

        service = await self.build_service(handler)

        session = await service.create_session(self.owner_id, "新对话")
        owned_after_create = self.store.owns(self.owner_id, "api_created")
        deleted = await service.delete_session(self.owner_id, "api_created")

        self.assertEqual(session, {"id": "api_created", "title": "新对话"})
        self.assertTrue(owned_after_create)
        self.assertTrue(deleted)
        self.assertFalse(self.store.owns(self.owner_id, "api_created"))

    async def test_run_pump_keeps_reasoning_approval_and_completion_in_order(self) -> None:
        requests: list[tuple[str, str, dict | None]] = []

        async def handler(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content) if request.content else None
            requests.append((request.method, request.url.path, body))
            if request.url.path == "/api/sessions/api_session_1/messages":
                return httpx.Response(
                    200,
                    json={
                        "object": "list",
                        "session_id": "api_session_1",
                        "data": [
                            {"role": "user", "content": "前一个问题"},
                            {"role": "assistant", "content": "前一个回答"},
                            {"role": "tool", "content": "不应进入 history"},
                        ],
                    },
                )
            if request.url.path == "/v1/runs":
                return httpx.Response(202, json={"run_id": "run_123", "status": "started"})
            if request.url.path == "/v1/runs/run_123/events":
                events = [
                    {"event": "reasoning.available", "run_id": "run_123", "text": "先确认范围"},
                    {
                        "event": "approval.request",
                        "run_id": "run_123",
                        "command": "test command",
                        "description": "需要授权",
                        "choices": ["once", "deny"],
                    },
                    {"event": "message.delta", "run_id": "run_123", "delta": "处理完成"},
                    {"event": "run.completed", "run_id": "run_123", "output": "处理完成"},
                ]
                payload = "".join(
                    f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
                    for event in events
                )
                return httpx.Response(200, text=payload, headers={"content-type": "text/event-stream"})
            raise AssertionError(f"unexpected request: {request.method} {request.url}")

        service = await self.build_service(handler)

        self.store.add(self.owner_id, "api_session_1")
        started = await service.start_run(self.owner_id, "api_session_1", "继续处理")
        snapshot = await self.wait_for_done("run_123")

        self.assertEqual(started, {"run_id": "run_123", "status": "started"})
        run_request = next(item for item in requests if item[1] == "/v1/runs")
        self.assertEqual(
            run_request[2],
            {
                "input": "继续处理",
                "session_id": "api_session_1",
                "conversation_history": [
                    {"role": "user", "content": "前一个问题"},
                    {"role": "assistant", "content": "前一个回答"},
                ],
            },
        )
        self.assertEqual(
            [event["event"] for event in snapshot["events"]],
            [
                "reasoning.available",
                "approval.request",
                "message.delta",
                "run.completed",
            ],
        )
        self.assertEqual([event["seq"] for event in snapshot["events"]], [1, 2, 3, 4])
        self.assertEqual(snapshot["status"], "completed")

    async def test_approval_is_forwarded_to_the_matching_run(self) -> None:
        async def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/api/sessions/api_approve/messages":
                return httpx.Response(200, json={"data": []})
            if request.url.path == "/v1/runs":
                return httpx.Response(202, json={"run_id": "run_approve", "status": "started"})
            if request.url.path == "/v1/runs/run_approve/events":
                return httpx.Response(
                    200,
                    text='data: {"event":"approval.request","run_id":"run_approve"}\n\n',
                )
            if request.url.path == "/v1/runs/run_approve/approval":
                self.assertEqual(request.method, "POST")
                self.assertEqual(json.loads(request.content), {"choice": "once"})
                return httpx.Response(
                    200,
                    json={
                        "object": "hermes.run.approval_response",
                        "run_id": "run_approve",
                        "choice": "once",
                        "resolved": 1,
                    },
                )
            raise AssertionError(f"unexpected request: {request.method} {request.url}")

        service = await self.build_service(handler)
        self.store.add(self.owner_id, "api_approve")
        await service.start_run(self.owner_id, "api_approve", "执行操作")

        response = await service.approve_run(self.owner_id, "run_approve", "once")

        self.assertEqual(
            response,
            {"run_id": "run_approve", "choice": "once", "resolved": 1},
        )

        with self.assertRaisesRegex(DashboardAgentError, "not found"):
            await service.approve_run(self.other_owner_id, "run_approve", "once")
        with self.assertRaisesRegex(DashboardAgentError, "not found"):
            await service.get_run_events(self.other_owner_id, "run_approve", after=0)
        with self.assertRaisesRegex(DashboardAgentError, "not found"):
            await service.stop_run(self.other_owner_id, "run_approve")

    async def test_second_run_for_same_session_is_rejected_while_first_is_active(self) -> None:
        release_events = asyncio.Event()

        class BlockingStream(httpx.AsyncByteStream):
            async def __aiter__(self):
                await release_events.wait()
                yield b'data: {"event":"run.completed","run_id":"run_busy","output":"ok"}\n\n'

        async def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/api/sessions/api_busy/messages":
                return httpx.Response(200, json={"data": []})
            if request.url.path == "/v1/runs":
                return httpx.Response(202, json={"run_id": "run_busy", "status": "started"})
            if request.url.path == "/v1/runs/run_busy/events":
                return httpx.Response(200, stream=BlockingStream())
            raise AssertionError(f"unexpected request: {request.method} {request.url}")

        service = await self.build_service(handler)
        self.store.add(self.owner_id, "api_busy")
        await service.start_run(self.owner_id, "api_busy", "第一个问题")

        with self.assertRaises(DashboardAgentSessionBusyError):
            await service.start_run(self.owner_id, "api_busy", "第二个问题")

        release_events.set()
        await self.wait_for_done("run_busy")


if __name__ == "__main__":
    unittest.main()
