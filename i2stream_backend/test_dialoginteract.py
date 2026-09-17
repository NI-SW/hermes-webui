from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException
from pydantic import SecretStr, ValidationError

os.environ.setdefault("SESSION_HMAC_SECRET", "session-secret-32-bytes-for-tests!!")
os.environ.setdefault("GATEWAY_BRIDGE_TOKEN", "gateway-token-32-bytes-for-tests!!!")

import chat_store
import auth
import db
import main
from agent_task import AgentTaskError
from datacop_client import DatacopClientError, DatacopProblem
from dialoginteract import (
    DialogInteractionConflictError,
    DialogInteractionService,
    MessageFeedbackRecord,
    WebUIDialogInteractionService,
)
from models import MessageFeedbackRequest, WebUIMessageFeedbackRequest


PROBLEM_JSON = """{
  "name": "规则同步失败",
  "description": "规则无法同步",
  "scenario": "同步规则",
  "trigger_method": "执行同步",
  "symptoms": "任务失败",
  "cause": "配置缺失",
  "solution": "补充配置",
  "verification": "重新同步成功",
  "notes": ""
}"""


class FakeUploader:
    def __init__(self) -> None:
        self.calls: list[DatacopProblem] = []
        self.failures_remaining = 0

    async def upload_problem(self, problem: DatacopProblem) -> int:
        self.calls.append(problem)
        if self.failures_remaining > 0:
            self.failures_remaining -= 1
            raise DatacopClientError("temporary upload failure")
        return 91


class FakeAgentRunner:
    def __init__(self, callback=None) -> None:
        self.callback = callback

    async def run_prompt(self, *, task_id: str, prompt: str) -> str:
        if self.callback is None:
            return PROBLEM_JSON
        return await self.callback(task_id=task_id, prompt=prompt)


class FakeFeedbackStore:
    def __init__(self) -> None:
        self.records_by_message: dict[tuple[str, str, int], MessageFeedbackRecord] = {}
        self.records_by_job: dict[str, MessageFeedbackRecord] = {}
        self.fail_incomplete_calls = 0

    def create(
        self,
        record: MessageFeedbackRecord,
    ) -> tuple[bool, MessageFeedbackRecord]:
        key = (record.client_id, record.conversation_id, record.message_id)
        existing = self.records_by_message.get(key)
        if existing is not None:
            return False, existing
        self.records_by_message[key] = record
        if record.job_id is not None:
            self.records_by_job[record.job_id] = record
        return True, record

    def get_by_message(
        self,
        client_id: str,
        conversation_id: str,
        message_id: int,
    ) -> MessageFeedbackRecord | None:
        return self.records_by_message.get((client_id, conversation_id, message_id))

    def get_by_job(self, client_id: str, job_id: str) -> MessageFeedbackRecord | None:
        record = self.records_by_job.get(job_id)
        return record if record is not None and record.client_id == client_id else None

    def update_job(
        self,
        job_id: str,
        *,
        status: str,
        datacop_problem_id: int | None,
        error: str | None,
    ) -> MessageFeedbackRecord:
        current = self.records_by_job[job_id]
        updated = MessageFeedbackRecord(
            client_id=current.client_id,
            conversation_id=current.conversation_id,
            message_id=current.message_id,
            feedback=current.feedback,
            status=status,
            job_id=current.job_id,
            datacop_problem_id=datacop_problem_id,
            error=error,
        )
        key = (updated.client_id, updated.conversation_id, updated.message_id)
        self.records_by_message[key] = updated
        self.records_by_job[job_id] = updated
        return updated

    def fail_incomplete(self, error: str) -> int:
        self.fail_incomplete_calls += 1
        changed = 0
        for job_id, record in tuple(self.records_by_job.items()):
            if record.status in {"queued", "summarizing", "uploading"}:
                self.update_job(
                    job_id,
                    status="failed",
                    datacop_problem_id=None,
                    error=error,
                )
                changed += 1
        return changed


class FailSuccessFeedbackStore(FakeFeedbackStore):
    def update_job(
        self,
        job_id: str,
        *,
        status: str,
        datacop_problem_id: int | None,
        error: str | None,
    ) -> MessageFeedbackRecord:
        if status == "succeeded":
            raise RuntimeError("database write failed")
        return super().update_job(
            job_id,
            status=status,
            datacop_problem_id=datacop_problem_id,
            error=error,
        )


class DialogInteractModelTests(unittest.TestCase):
    def test_feedback_only_accepts_like_or_dislike(self) -> None:
        self.assertEqual(MessageFeedbackRequest(feedback="like").feedback, "like")
        self.assertEqual(MessageFeedbackRequest(feedback="dislike").feedback, "dislike")

        with self.assertRaises(ValidationError):
            MessageFeedbackRequest(feedback="neutral")

    def test_webui_feedback_requires_a_typed_snapshot(self) -> None:
        request = WebUIMessageFeedbackRequest(
            source_instance_id="f" * 64,
            feedback="like",
            messages=[
                {"role": "user", "content": "question"},
                {"role": "assistant", "content": "answer"},
            ],
        )

        self.assertEqual(request.feedback, "like")
        self.assertEqual(request.messages[-1].role, "assistant")

        with self.assertRaises(ValidationError):
            WebUIMessageFeedbackRequest(
                source_instance_id="f" * 64,
                feedback="like",
                messages=[],
            )


class WebUIFeedbackAuthTests(unittest.TestCase):
    def test_webui_feedback_auth_is_required_and_dedicated(self) -> None:
        with patch.object(
            auth.settings,
            "webui_feedback_bridge_token",
            SecretStr("native-feedback-token"),
        ):
            auth.require_webui_feedback_auth("Bearer native-feedback-token")
            with self.assertRaises(HTTPException) as missing:
                auth.require_webui_feedback_auth(None)
            with self.assertRaises(HTTPException) as wrong:
                auth.require_webui_feedback_auth("Bearer different-token")

        self.assertEqual(missing.exception.status_code, 401)
        self.assertEqual(wrong.exception.status_code, 401)

    def test_webui_feedback_auth_fails_closed_when_unconfigured(self) -> None:
        with patch.object(
            auth.settings,
            "webui_feedback_bridge_token",
            SecretStr(""),
        ):
            with self.assertRaises(HTTPException) as raised:
                auth.require_webui_feedback_auth("Bearer anything")

        self.assertEqual(raised.exception.status_code, 503)


class DialogInteractionServiceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.agent_calls: list[tuple[str, str]] = []

        async def run_prompt(*, task_id: str, prompt: str) -> str:
            self.agent_calls.append((task_id, prompt))
            return PROBLEM_JSON

        self.agent_runner = FakeAgentRunner(run_prompt)
        self.uploader = FakeUploader()
        self.feedback_store = FakeFeedbackStore()
        self.service = DialogInteractionService(
            uploader=self.uploader,
            store=self.feedback_store,
            agent_runner=self.agent_runner,
            queue_size=4,
            job_ttl_seconds=900,
        )
        self.messages = [
            {"id": 1, "role": "user", "content": "规则为什么同步失败？", "payload": None},
            {
                "id": 2,
                "role": "assistant",
                "content": "缺少配置，请补充后重新同步。",
                "payload": {
                    "files": [
                        {
                            "name": "report.html",
                            "description": "同步诊断报告",
                            "url": "http://secret-download-url",
                            "token": "secret-token",
                        }
                    ]
                },
            },
        ]

    async def test_same_like_job_is_processed_only_once(self) -> None:
        first = await self.service.submit(
            client_id="c" * 64,
            conversation_id="conversation-1",
            message_id=2,
            feedback="like",
            messages=self.messages,
        )
        second = await self.service.submit(
            client_id="c" * 64,
            conversation_id="conversation-1",
            message_id=2,
            feedback="like",
            messages=self.messages,
        )

        self.assertEqual(first["status"], "queued")
        self.assertEqual(first["conversation_id"], "conversation-1")
        self.assertEqual(second["job_id"], first["job_id"])
        self.assertEqual(self.service.pending_count, 1)

        await self.service.process_next()
        status = self.service.get_job("c" * 64, str(first["job_id"]))

        self.assertEqual(
            status,
            {
                "code": 0,
                "job_id": first["job_id"],
                "status": "succeeded",
                "feedback": "like",
                "conversation_id": "conversation-1",
                "message_id": 2,
                "datacop_problem_id": 91,
                "error": None,
            },
        )
        self.assertEqual(len(self.agent_calls), 1)
        prompt = self.agent_calls[0][1]
        self.assertIn("规则为什么同步失败", prompt)
        self.assertIn("report.html", prompt)
        self.assertIn("同步诊断报告", prompt)
        self.assertNotIn("secret-download-url", prompt)
        self.assertNotIn("secret-token", prompt)
        self.assertEqual(len(self.uploader.calls), 1)
        problem = self.uploader.calls[0]
        self.assertEqual(problem.name, "规则同步失败")
        persisted = self.feedback_store.get_by_job("c" * 64, str(first["job_id"]))
        self.assertEqual(persisted.status, "succeeded")
        self.assertEqual(persisted.datacop_problem_id, 91)

    async def test_dislike_is_acknowledged_without_creating_background_job(self) -> None:
        response = await self.service.submit(
            client_id="c" * 64,
            conversation_id="conversation-1",
            message_id=2,
            feedback="dislike",
            messages=self.messages,
        )

        self.assertEqual(response["status"], "received")
        self.assertNotIn("job_id", response)
        self.assertEqual(self.service.pending_count, 0)
        stored = self.feedback_store.get_by_message("c" * 64, "conversation-1", 2)
        self.assertEqual(stored.feedback, "dislike")
        self.assertEqual(stored.status, "received")

    async def test_failed_job_is_terminal_and_cannot_be_requeued(self) -> None:
        self.uploader.failures_remaining = 1
        response = await self.service.submit(
            client_id="c" * 64,
            conversation_id="conversation-1",
            message_id=2,
            feedback="like",
            messages=self.messages,
        )
        await self.service.process_next()
        failed = self.service.get_job("c" * 64, str(response["job_id"]))
        self.assertEqual(failed["status"], "failed")

        retried = await self.service.submit(
            client_id="c" * 64,
            conversation_id="conversation-1",
            message_id=2,
            feedback="like",
            messages=self.messages,
        )
        self.assertEqual(retried["job_id"], response["job_id"])
        self.assertEqual(retried["status"], "failed")
        self.assertEqual(self.service.pending_count, 0)
        self.assertEqual(len(self.agent_calls), 1)
        self.assertEqual(len(self.uploader.calls), 1)
        persisted = self.feedback_store.get_by_job("c" * 64, str(response["job_id"]))
        self.assertEqual(persisted.status, "failed")

    async def test_job_status_is_scoped_to_its_client(self) -> None:
        response = await self.service.submit(
            client_id="c" * 64,
            conversation_id="conversation-1",
            message_id=2,
            feedback="like",
            messages=self.messages,
        )

        self.assertIsNone(self.service.get_job("d" * 64, str(response["job_id"])))

    async def test_invalid_agent_json_does_not_leak_output_in_public_error(self) -> None:
        leaked_text = "private conversation fragment"

        async def invalid_agent_runner(
            *,
            task_id: str,
            prompt: str,
        ) -> str:
            return f'{{"name":"{leaked_text}"}}'

        service = DialogInteractionService(
            uploader=self.uploader,
            store=FakeFeedbackStore(),
            agent_runner=FakeAgentRunner(invalid_agent_runner),
        )
        response = await service.submit(
            client_id="c" * 64,
            conversation_id="conversation-1",
            message_id=2,
            feedback="like",
            messages=self.messages,
        )

        with self.assertLogs("dialoginteract", level="WARNING"):
            await service.process_next()
        status = service.get_job("c" * 64, str(response["job_id"]))

        self.assertEqual(status["status"], "failed")
        self.assertEqual(status["error"], "Agent summary did not match the DataCop format")
        self.assertNotIn(leaked_text, str(status["error"]))

    async def test_blank_dependency_error_is_persisted_as_explicit_failure(self) -> None:
        async def failed_agent_runner(
            *,
            task_id: str,
            prompt: str,
        ) -> str:
            raise AgentTaskError("")

        store = FakeFeedbackStore()
        service = DialogInteractionService(
            uploader=self.uploader,
            store=store,
            agent_runner=FakeAgentRunner(failed_agent_runner),
        )
        response = await service.submit(
            client_id="c" * 64,
            conversation_id="conversation-1",
            message_id=2,
            feedback="like",
            messages=self.messages,
        )

        await service.process_next()

        status = service.get_job("c" * 64, str(response["job_id"]))
        self.assertEqual(status["status"], "failed")
        self.assertEqual(status["error"], "Background DataCop task failed")

    async def test_success_status_write_failure_does_not_crash_worker_boundary(self) -> None:
        store = FailSuccessFeedbackStore()
        service = DialogInteractionService(
            uploader=self.uploader,
            store=store,
            agent_runner=self.agent_runner,
        )
        response = await service.submit(
            client_id="c" * 64,
            conversation_id="conversation-1",
            message_id=2,
            feedback="like",
            messages=self.messages,
        )

        with self.assertLogs("dialoginteract", level="ERROR"):
            await service.process_next()

        status = service.get_job("c" * 64, str(response["job_id"]))
        self.assertEqual(status["status"], "failed")
        self.assertEqual(status["error"], "Background DataCop task failed")

    async def test_finished_job_status_remains_after_memory_ttl(self) -> None:
        now = 100.0
        store = FakeFeedbackStore()
        service = DialogInteractionService(
            uploader=self.uploader,
            store=store,
            agent_runner=self.agent_runner,
            job_ttl_seconds=10,
            clock=lambda: now,
        )
        response = await service.submit(
            client_id="c" * 64,
            conversation_id="conversation-1",
            message_id=2,
            feedback="like",
            messages=self.messages,
        )
        await service.process_next()
        self.assertIsNotNone(service.get_job("c" * 64, str(response["job_id"])))

        now = 110.0
        restored = service.get_job("c" * 64, str(response["job_id"]))
        self.assertEqual(restored["status"], "succeeded")
        self.assertEqual(restored["datacop_problem_id"], 91)

    async def test_first_feedback_is_immutable(self) -> None:
        await self.service.submit(
            client_id="c" * 64,
            conversation_id="conversation-1",
            message_id=2,
            feedback="dislike",
            messages=self.messages,
        )

        with self.assertRaises(DialogInteractionConflictError):
            await self.service.submit(
                client_id="c" * 64,
                conversation_id="conversation-1",
                message_id=2,
                feedback="like",
                messages=self.messages,
            )

        self.assertEqual(self.service.pending_count, 0)
        self.assertEqual(len(self.agent_calls), 0)

    async def test_persisted_feedback_prevents_new_agent_task_after_memory_expiry(self) -> None:
        first = await self.service.submit(
            client_id="c" * 64,
            conversation_id="conversation-1",
            message_id=2,
            feedback="like",
            messages=self.messages,
        )
        await self.service.process_next()
        second_service = DialogInteractionService(
            uploader=self.uploader,
            store=self.feedback_store,
            agent_runner=self.agent_runner,
        )

        restored = await second_service.submit(
            client_id="c" * 64,
            conversation_id="conversation-1",
            message_id=2,
            feedback="like",
            messages=self.messages,
        )

        self.assertEqual(restored["job_id"], first["job_id"])
        self.assertEqual(restored["status"], "succeeded")
        self.assertEqual(second_service.pending_count, 0)
        self.assertEqual(len(self.agent_calls), 1)

    async def test_service_start_fails_persisted_incomplete_jobs(self) -> None:
        self.feedback_store.create(
            MessageFeedbackRecord(
                client_id="c" * 64,
                conversation_id="conversation-1",
                message_id=2,
                feedback="like",
                status="summarizing",
                job_id="job-before-restart",
                datacop_problem_id=None,
                error=None,
            )
        )

        await self.service.start()
        await self.service.stop()

        restored = self.feedback_store.get_by_job("c" * 64, "job-before-restart")
        self.assertEqual(restored.status, "failed")
        self.assertEqual(self.feedback_store.fail_incomplete_calls, 1)


class WebUIDialogInteractionServiceTests(unittest.IsolatedAsyncioTestCase):
    async def test_webui_service_reuses_pipeline_with_native_identifiers(self) -> None:
        store = FakeFeedbackStore()
        uploader = FakeUploader()

        async def agent_runner(
            *,
            task_id: str,
            prompt: str,
        ) -> str:
            self.assertIn("native question", prompt)
            return PROBLEM_JSON

        service = WebUIDialogInteractionService(
            uploader=uploader,
            store=store,
            agent_runner=FakeAgentRunner(agent_runner),
        )
        response = await service.submit(
            source_instance_id="f" * 64,
            session_id="webui-session-1",
            message_ref="a" * 64,
            feedback="like",
            messages=[
                {"role": "user", "content": "native question"},
                {"role": "assistant", "content": "native answer"},
            ],
        )
        duplicate = await service.submit(
            source_instance_id="f" * 64,
            session_id="webui-session-1",
            message_ref="a" * 64,
            feedback="like",
            messages=[{"role": "assistant", "content": "ignored duplicate"}],
        )

        self.assertEqual(response["session_id"], "webui-session-1")
        self.assertEqual(response["message_ref"], "a" * 64)
        self.assertNotIn("conversation_id", response)
        self.assertEqual(duplicate["job_id"], response["job_id"])
        self.assertEqual(service.pending_count, 1)

        await service.process_next()
        status = service.get_job("f" * 64, str(response["job_id"]))

        self.assertEqual(status["status"], "succeeded")
        self.assertEqual(status["session_id"], "webui-session-1")
        self.assertEqual(status["message_ref"], "a" * 64)
        self.assertEqual(status["datacop_problem_id"], 91)
        self.assertIsNone(service.get_job("e" * 64, str(response["job_id"])))

    async def test_webui_submit_returns_complete_synchronous_failure(self) -> None:
        service = WebUIDialogInteractionService(
            uploader=None,
            store=FakeFeedbackStore(),
            agent_runner=FakeAgentRunner(),
        )

        response = await service.submit(
            source_instance_id="f" * 64,
            session_id="webui-session-1",
            message_ref="d" * 64,
            feedback="like",
            messages=[{"role": "assistant", "content": "answer"}],
        )

        self.assertEqual(response["status"], "failed")
        self.assertEqual(response["error"], "DataCop feedback is not configured")
        self.assertIsNone(response["datacop_problem_id"])

    async def test_webui_first_feedback_is_immutable(self) -> None:
        service = WebUIDialogInteractionService(
            uploader=FakeUploader(),
            store=FakeFeedbackStore(),
            agent_runner=FakeAgentRunner(),
        )
        await service.submit(
            source_instance_id="f" * 64,
            session_id="webui-session-1",
            message_ref="b" * 64,
            feedback="dislike",
            messages=[{"role": "assistant", "content": "answer"}],
        )

        with self.assertRaises(DialogInteractionConflictError):
            await service.submit(
                source_instance_id="f" * 64,
                session_id="webui-session-1",
                message_ref="b" * 64,
                feedback="like",
                messages=[{"role": "assistant", "content": "answer"}],
            )

    async def test_webui_snapshot_limit_applies_before_dislike_is_stored(self) -> None:
        store = FakeFeedbackStore()
        service = WebUIDialogInteractionService(
            uploader=FakeUploader(),
            store=store,
            agent_runner=FakeAgentRunner(),
        )

        with self.assertRaisesRegex(ValueError, "too large"):
            await service.submit(
                source_instance_id="f" * 64,
                session_id="webui-session-1",
                message_ref="c" * 64,
                feedback="dislike",
                messages=[{"role": "assistant", "content": "x" * (256 * 1024)}],
            )

        self.assertIsNone(
            store.get_by_message("f" * 64, "webui-session-1", "c" * 64)
        )


class DialogInteractRouteTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        db.chat_schema_ready = False

    def tearDown(self) -> None:
        db.chat_schema_ready = False

    async def test_feedback_route_passes_visible_snapshot_to_memory_service(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            sqlite_path = Path(temp_dir) / "chat.db"
            with patch.object(db, "SQLITE_DB_PATH", sqlite_path):
                client_id = chat_store.issue_chat_client_id()
                chat_store.insert_chat_message(client_id, "conversation-1", "user", "question")
                message_id = chat_store.insert_chat_message(
                    client_id,
                    "conversation-1",
                    "assistant",
                    "answer",
                )
                chat_store.insert_chat_message(client_id, "conversation-1", "user", "later")
                expected = {
                    "code": 0,
                    "status": "queued",
                    "job_id": "job-1",
                    "feedback": "like",
                    "conversation_id": "conversation-1",
                    "message_id": message_id,
                }
                service = SimpleNamespace(submit=AsyncMock(return_value=expected))
                with patch.object(main, "dialog_interaction_service", service):
                    response = await main.submit_message_feedback(
                        "conversation-1",
                        message_id,
                        MessageFeedbackRequest(feedback="like"),
                        client_id,
                        None,
                    )

        self.assertEqual(response, expected)
        call = service.submit.await_args
        self.assertEqual(call.kwargs["client_id"], client_id)
        self.assertEqual(call.kwargs["conversation_id"], "conversation-1")
        self.assertEqual(call.kwargs["message_id"], message_id)
        self.assertEqual(call.kwargs["feedback"], "like")
        self.assertEqual([message["content"] for message in call.kwargs["messages"]], ["question", "answer"])

    async def test_feedback_rejects_other_clients_conversations_and_user_messages(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            sqlite_path = Path(temp_dir) / "chat.db"
            with patch.object(db, "SQLITE_DB_PATH", sqlite_path):
                client_id = chat_store.issue_chat_client_id()
                other_client_id = chat_store.issue_chat_client_id()
                assistant_id = chat_store.insert_chat_message(
                    client_id,
                    "conversation-1",
                    "assistant",
                    "answer",
                )
                user_id = chat_store.insert_chat_message(
                    client_id,
                    "conversation-1",
                    "user",
                    "question",
                )

                cases = (
                    (other_client_id, "conversation-1", assistant_id),
                    (client_id, "conversation-2", assistant_id),
                    (client_id, "conversation-1", user_id),
                )
                for scoped_client, conversation_id, message_id in cases:
                    with self.subTest(
                        client_id=scoped_client,
                        conversation_id=conversation_id,
                        message_id=message_id,
                    ):
                        with self.assertRaises(HTTPException) as raised:
                            await main.submit_message_feedback(
                                conversation_id,
                                message_id,
                                MessageFeedbackRequest(feedback="dislike"),
                                scoped_client,
                                None,
                            )
                        self.assertEqual(raised.exception.status_code, 404)

    async def test_feedback_rejects_message_ids_outside_the_browser_safe_range(self) -> None:
        for message_id in (0, (1 << 53)):
            with self.subTest(message_id=message_id):
                with self.assertRaises(HTTPException) as raised:
                    await main.submit_message_feedback(
                        "conversation-1",
                        message_id,
                        MessageFeedbackRequest(feedback="like"),
                        "c" * 64,
                        None,
                    )

                self.assertEqual(raised.exception.status_code, 400)

    async def test_job_status_route_is_scoped_to_client(self) -> None:
        service = SimpleNamespace(
            get_job=lambda client_id, job_id: (
                {"code": 0, "job_id": job_id, "status": "succeeded"}
                if client_id == "c" * 64
                else None
            )
        )
        with patch.object(main, "dialog_interaction_service", service):
            response = await main.get_dialog_interaction_job("job-1", "c" * 64, None)
            with self.assertRaises(HTTPException) as raised:
                await main.get_dialog_interaction_job("job-1", "d" * 64, None)

        self.assertEqual(response["status"], "succeeded")
        self.assertEqual(raised.exception.status_code, 404)

    def test_feedback_and_job_status_routes_are_registered(self) -> None:
        paths = {route.path for route in main.app.routes}
        self.assertIn(
            "/api/conversations/{conversation_id}/messages/{message_id}/feedback",
            paths,
        )
        self.assertIn("/api/dialog-interactions/{job_id}", paths)
        self.assertIn(
            "/api/webui/sessions/{session_id}/messages/{message_ref}/feedback",
            paths,
        )
        self.assertIn("/api/webui/dialog-interactions/{job_id}", paths)

    async def test_webui_feedback_route_passes_server_snapshot(self) -> None:
        expected = {
            "code": 0,
            "status": "queued",
            "job_id": "native-job-1",
            "feedback": "like",
            "session_id": "webui-session-1",
            "message_ref": "a" * 64,
        }
        service = SimpleNamespace(submit=AsyncMock(return_value=expected))
        payload = WebUIMessageFeedbackRequest(
            source_instance_id="f" * 64,
            feedback="like",
            messages=[
                {"role": "user", "content": "question"},
                {"role": "assistant", "content": "answer"},
            ],
        )
        with patch.object(main, "webui_dialog_interaction_service", service):
            response = await main.submit_webui_message_feedback(
                "webui-session-1",
                "a" * 64,
                payload,
                None,
            )

        self.assertEqual(response, expected)
        call = service.submit.await_args
        self.assertEqual(call.kwargs["session_id"], "webui-session-1")
        self.assertEqual(call.kwargs["source_instance_id"], "f" * 64)
        self.assertEqual(call.kwargs["message_ref"], "a" * 64)
        self.assertEqual(call.kwargs["messages"][-1], {"role": "assistant", "content": "answer", "payload": None})

    async def test_webui_feedback_route_validates_identifiers_and_last_role(self) -> None:
        valid_payload = WebUIMessageFeedbackRequest(
            source_instance_id="f" * 64,
            feedback="like",
            messages=[{"role": "assistant", "content": "answer"}],
        )
        user_last_payload = WebUIMessageFeedbackRequest(
            source_instance_id="f" * 64,
            feedback="like",
            messages=[{"role": "user", "content": "question"}],
        )
        cases = (
            ("session/escape", "a" * 64, valid_payload),
            ("webui-session-1", "not-a-ref", valid_payload),
            ("webui-session-1", "a" * 64, user_last_payload),
        )
        for session_id, message_ref, payload in cases:
            with self.subTest(session_id=session_id, message_ref=message_ref):
                with self.assertRaises(HTTPException) as raised:
                    await main.submit_webui_message_feedback(
                        session_id,
                        message_ref,
                        payload,
                        None,
                    )
                self.assertEqual(raised.exception.status_code, 400)

    async def test_webui_job_status_route_reads_native_service(self) -> None:
        service = SimpleNamespace(
            get_job=lambda source_instance_id, job_id: (
                {"code": 0, "job_id": job_id, "status": "succeeded"}
                if source_instance_id == "f" * 64 and job_id == "native-job-1"
                else None
            )
        )
        with patch.object(main, "webui_dialog_interaction_service", service):
            response = await main.get_webui_dialog_interaction_job(
                "native-job-1", "f" * 64, None
            )
            with self.assertRaises(HTTPException) as raised:
                await main.get_webui_dialog_interaction_job("unknown", "f" * 64, None)

        self.assertEqual(response["status"], "succeeded")
        self.assertEqual(raised.exception.status_code, 404)


if __name__ == "__main__":
    unittest.main()
