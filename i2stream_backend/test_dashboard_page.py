from __future__ import annotations

import io
import os
import re
import unittest
from unittest.mock import AsyncMock, patch

import httpx

os.environ.setdefault("VECTOR_SEARCH_HOST", "http://127.0.0.1:8900")
os.environ.setdefault("SESSION_HMAC_SECRET", "session-secret-32-bytes-for-tests!!")
os.environ.setdefault("GATEWAY_BRIDGE_TOKEN", "gateway-token-32-bytes-for-tests!!!")

import main
from dashboard_identity import (
    DASHBOARD_USER_COOKIE_MAX_AGE_SECONDS,
    DASHBOARD_USER_COOKIE_NAME,
)


class DashboardPageTests(unittest.IsolatedAsyncioTestCase):
    def test_dashboard_route_is_registered(self) -> None:
        paths = {route.path for route in main.app.routes}

        self.assertIn("/", paths)
        self.assertIn("/dashboard", paths)
        self.assertNotIn("/knowledge", paths)
        self.assertIn("/reports", paths)
        self.assertIn("/chat", paths)
        self.assertIn("/chat-history", paths)
        self.assertNotIn("/dashboard/knowledge/upload", paths)
        self.assertNotIn("/dashboard/knowledge/files", paths)
        self.assertNotIn("/dashboard/reports", paths)
        self.assertNotIn("/dashboard/chat-history", paths)
        self.assertIn("/knowledge/files/delete", paths)

    def test_dashboard_agent_api_routes_are_registered(self) -> None:
        paths = {route.path for route in main.app.routes}

        self.assertIn("/api/dashboard-agent/sessions", paths)
        self.assertIn("/api/dashboard-agent/sessions/{session_id}/messages", paths)
        self.assertIn("/api/dashboard-agent/sessions/{session_id}/active-run", paths)
        self.assertIn("/api/dashboard-agent/sessions/{session_id}/runs", paths)
        self.assertIn("/api/dashboard-agent/runs/{run_id}/events", paths)
        self.assertIn("/api/dashboard-agent/runs/{run_id}/approval", paths)
        self.assertIn("/api/dashboard-agent/runs/{run_id}/stop", paths)

    async def test_root_page_redirects_to_dashboard(self) -> None:
        response = await main.root_page()

        self.assertEqual(response.status_code, 307)
        self.assertEqual(response.headers["location"], "/dashboard")

    async def test_dashboard_page_contains_navigation_and_api_hooks(self) -> None:
        with patch.object(main, "list_vector_files", return_value=[]):
            response = await main.dashboard_page()

        self.assertEqual(response.status_code, 200)
        body = response.body.decode("utf-8")
        self.assertIn("Hermes Dashboard", body)
        self.assertIn("data-view=\"knowledge\"", body)
        self.assertNotIn("data-view=\"knowledge-files\"", body)
        self.assertIn("data-view=\"reports\"", body)
        self.assertIn("data-view=\"chat\"", body)
        self.assertIn("data-view=\"chat-history\"", body)
        self.assertIn('href="/dashboard"', body)
        self.assertNotIn('href="/knowledge"', body)
        self.assertIn('href="/reports"', body)
        self.assertIn('href="/chat"', body)
        self.assertIn('href="/chat-history"', body)
        self.assertIn('method="post" action="/dashboard" enctype="multipart/form-data"', body)
        self.assertIn('id="knowledge-selection"', body)
        self.assertIn('id="knowledge-selection-list"', body)
        self.assertIn('knowledgeFiles.addEventListener("change"', body)

    async def test_non_upload_pages_do_not_include_file_selection_script(self) -> None:
        with patch.object(main, "list_report_files", return_value=[]):
            response = await main.reports_page()

        body = response.body.decode("utf-8")
        self.assertNotIn('id="knowledge-selection"', body)
        self.assertNotIn("knowledgeFiles.addEventListener", body)

    async def test_chat_page_contains_session_timeline_composer_and_approval_hooks(self) -> None:
        response = await main.dashboard_chat_page()

        self.assertEqual(response.status_code, 200)
        body = response.body.decode("utf-8")
        self.assertIn('data-view="chat" aria-selected="true"', body)
        self.assertIn('id="dashboard-chat-sessions"', body)
        self.assertIn('id="dashboard-chat-messages"', body)
        self.assertIn('id="dashboard-chat-composer"', body)
        self.assertIn('/api/dashboard-agent/sessions', body)
        self.assertIn('approval.request', body)
        self.assertNotIn("DASHBOARD_HERMES_API_KEY", body)
        self.assertNotIn("API_SERVER_KEY", body)

    async def test_chat_page_creates_session_on_first_message_instead_of_new_button(self) -> None:
        response = await main.dashboard_chat_page()

        body = response.body.decode("utf-8")
        self.assertIn('const beginNewDraft = () => {', body)
        self.assertIn('newButton.addEventListener("click", beginNewDraft);', body)
        self.assertIn('const sessionId = state.activeSessionId || await createSession(message);', body)
        self.assertNotIn('newButton.addEventListener("click", createSession);', body)
        self.assertNotIn('if (!state.activeSessionId || state.activeRunId) return;', body)

    async def test_chat_page_removes_welcome_and_keeps_composer_inside_viewport(self) -> None:
        response = await main.dashboard_chat_page()

        body = response.body.decode("utf-8")
        workspace_rule = re.search(r"\.chat-workspace\s*\{([^}]*)\}", body)
        self.assertIsNotNone(workspace_rule)
        self.assertIn("min-height: 0;", workspace_rule.group(1))
        self.assertIn("overflow: hidden;", workspace_rule.group(1))

        start_run = body.index("const startRun = async (message) => {")
        remove_welcome = body.index(
            'messagesElement.querySelector(".chat-welcome")?.remove();',
            start_run,
        )
        append_user_turn = body.index('createTurn("user", message);', start_run)
        self.assertLess(remove_welcome, append_user_turn)

    async def test_dashboard_chat_uses_signed_cookie_identity_and_renews_it(self) -> None:
        list_sessions = AsyncMock(return_value=[])
        transport = httpx.ASGITransport(app=main.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://dashboard.test") as client:
            with patch.object(main.dashboard_agent_service, "list_sessions", list_sessions):
                page_response = await client.get("/chat")
                api_response = await client.get("/api/dashboard-agent/sessions")

        cookie_value = page_response.cookies[DASHBOARD_USER_COOKIE_NAME]
        owner_id = cookie_value.partition(".")[0]
        self.assertEqual(len(owner_id), 64)
        self.assertIn("HttpOnly", page_response.headers["set-cookie"])
        self.assertIn("SameSite=lax", page_response.headers["set-cookie"])
        self.assertIn(
            f"Max-Age={DASHBOARD_USER_COOKIE_MAX_AGE_SECONDS}",
            page_response.headers["set-cookie"],
        )
        self.assertIn(DASHBOARD_USER_COOKIE_NAME, api_response.headers["set-cookie"])
        list_sessions.assert_awaited_once_with(owner_id)

    async def test_dashboard_run_route_keeps_api_and_run_status_separate(self) -> None:
        start_run = AsyncMock(return_value={"run_id": "run_123", "status": "started"})
        with patch.object(main.dashboard_agent_service, "start_run", start_run):
            payload = await main.api_start_dashboard_agent_run(
                "api_session_1",
                main.DashboardRunRequest(message="检查同步状态"),
                "a" * 64,
            )

        start_run.assert_awaited_once_with("a" * 64, "api_session_1", "检查同步状态")
        self.assertEqual(
            payload,
            {
                "code": 0,
                "status": "success",
                "run_id": "run_123",
                "run_status": "started",
            },
        )

    async def test_dashboard_page_renders_one_page_per_route(self) -> None:
        expected_reports = [
            {
                "token": "a" * 16,
                "name": "report.html",
                "url": "/api/reports/" + "a" * 16 + "/content",
                "media_type": "text/html",
                "size": 2048,
                "created_at": 1783500000.0,
                "description": "diagnostic report",
            }
        ]

        with patch.object(main, "list_report_files", return_value=expected_reports):
            response = await main.reports_page()

        self.assertEqual(response.status_code, 200)
        body = response.body.decode("utf-8")
        self.assertIn('data-view="reports" aria-selected="true"', body)
        self.assertIn('<article id="view-reports" class="view" data-view-panel="reports">', body)
        self.assertIn("report.html", body)
        self.assertIn("diagnostic report", body)
        self.assertIn('action="/reports/aaaaaaaaaaaaaaaa/delete"', body)
        self.assertNotIn('id="view-knowledge"', body)
        self.assertNotIn('id="view-knowledge-files"', body)
        self.assertNotIn('id="view-chat-history"', body)
        self.assertNotIn(" hidden>", body)

    async def test_dashboard_page_renders_upload_and_knowledge_files_together(self) -> None:
        async def fake_list_vector_files():
            return [
                {
                    "file_id": "1783409846_manual.docx",
                    "display_name": "manual.docx",
                    "file_type": "docx",
                    "file_size": 120,
                    "upload_time": "2026-07-07T10:00:00",
                    "total_chunks": 3,
                }
            ]

        with patch.object(main, "list_vector_files", fake_list_vector_files):
            response = await main.dashboard_page()

        self.assertEqual(response.status_code, 200)
        body = response.body.decode("utf-8")
        self.assertIn('<article id="view-knowledge" class="view" data-view-panel="knowledge">', body)
        self.assertIn('id="knowledge-form"', body)
        self.assertNotIn('id="view-knowledge-files"', body)
        self.assertIn("manual.docx", body)
        self.assertIn("1783409846_manual.docx", body)
        self.assertIn("已选择 0 / 1", body)
        self.assertIn('id="knowledge-files-select-all"', body)
        self.assertIn('id="knowledge-files-delete-selected"', body)
        self.assertIn('method="post" action="/knowledge/files/delete"', body)
        self.assertIn(
            'class="knowledge-file-checkbox" type="checkbox" name="file_id" '
            'value="1783409846_manual.docx"',
            body,
        )
        self.assertIn('knowledgeBatchForm.addEventListener("submit"', body)
        self.assertIn('method="post" action="/knowledge/files/1783409846_manual.docx/delete"', body)

    async def test_chat_history_page_renders_query_result_on_server(self) -> None:
        expected_client_id = "e734d08e857a13db8956330984b3b0d7e240c6ffd41441c098a69d7213bc4caf"
        expected_conversations = [
            {
                "conversation_id": "conversation-1",
                "messages": [
                    {"role": "user", "content": "hello", "created_at": "2026-07-08 10:00:00"},
                    {"role": "assistant", "content": "world", "created_at": "2026-07-08 10:00:01"},
                ],
            }
        ]

        with patch.object(main, "list_client_chat_history", return_value=expected_conversations) as list_history:
            response = await main.chat_history_page(expected_client_id)

        list_history.assert_called_once_with(expected_client_id)
        self.assertEqual(response.status_code, 200)
        body = response.body.decode("utf-8")
        self.assertIn(f'value="{expected_client_id}"', body)
        self.assertIn("查询完成：1 个会话。", body)
        self.assertIn("conversation-1", body)
        self.assertIn("hello", body)
        self.assertIn("world", body)

    def test_legacy_api_routes_are_not_registered(self) -> None:
        paths = {route.path for route in main.app.routes}

        self.assertNotIn("/v1/responses", paths)
        self.assertNotIn("/api/communicate", paths)
        self.assertNotIn("/api/files", paths)
        self.assertNotIn("/files/{token}", paths)
        self.assertNotIn("/api/inbox/files", paths)
        self.assertNotIn("/api/inbox/files/{token}", paths)
        self.assertNotIn("/api/chat/messages", paths)
        self.assertNotIn("/api/chat/history", paths)
        self.assertNotIn("/api/chat/client-id", paths)
        self.assertNotIn("/api/chat/clear", paths)
        self.assertNotIn("/api/chat/progress/{request_id}", paths)

    async def test_knowledge_files_api_wraps_vector_file_list(self) -> None:
        expected_files = [
            {
                "file_id": "1783409846_manual.docx",
                "display_name": "manual.docx",
                "file_type": "docx",
                "file_size": 120,
                "upload_time": "2026-07-07T10:00:00",
                "total_chunks": 3,
            }
        ]

        async def fake_list_vector_files():
            return expected_files

        with patch.object(main, "list_vector_files", fake_list_vector_files):
            payload = await main.api_list_knowledge_files()

        self.assertEqual(
            payload,
            {
                "code": 0,
                "status": "success",
                "files": expected_files,
                "total": 1,
            },
        )

    async def test_knowledge_files_delete_api_wraps_vector_file_delete(self) -> None:
        expected_file = {"file_id": "1783409846_manual.docx", "operation_id": 42}

        async def fake_delete_vector_file(file_id):
            self.assertEqual(file_id, "1783409846_manual.docx")
            return expected_file

        with patch.object(main, "delete_vector_file", fake_delete_vector_file):
            payload = await main.api_delete_knowledge_file("1783409846_manual.docx")

        self.assertEqual(
            payload,
            {
                "code": 0,
                "status": "success",
                "file": expected_file,
            },
        )

    async def test_dashboard_knowledge_delete_form_redirects_to_dashboard(self) -> None:
        async def fake_delete_vector_file(file_id):
            self.assertEqual(file_id, "1783409846_manual.docx")
            return {"file_id": file_id}

        with patch.object(main, "delete_vector_file", fake_delete_vector_file):
            response = await main.dashboard_delete_knowledge_file("1783409846_manual.docx")

        self.assertEqual(response.status_code, 303)
        self.assertEqual(response.headers["location"], "/dashboard")

    async def test_dashboard_knowledge_batch_delete_deletes_each_selected_file(self) -> None:
        deleted_file_ids = []

        async def fake_delete_vector_file(file_id):
            deleted_file_ids.append(file_id)
            return {"file_id": file_id}

        with patch.object(main, "delete_vector_file", fake_delete_vector_file):
            response = await main.dashboard_delete_knowledge_files(
                [
                    "1783409846_manual.docx",
                    "1783409847_guide.pdf",
                ]
            )

        self.assertEqual(
            deleted_file_ids,
            [
                "1783409846_manual.docx",
                "1783409847_guide.pdf",
            ],
        )
        self.assertEqual(response.status_code, 303)
        self.assertEqual(response.headers["location"], "/dashboard")

    async def test_dashboard_knowledge_batch_delete_rejects_empty_selection(self) -> None:
        with self.assertRaises(main.HTTPException) as raised:
            await main.dashboard_delete_knowledge_files([])

        self.assertEqual(raised.exception.status_code, 400)
        self.assertEqual(raised.exception.detail, "Select at least one knowledge file")

    async def test_dashboard_report_delete_form_redirects_to_reports_page(self) -> None:
        with patch.object(main, "delete_inbox_record", return_value=True) as delete_record:
            response = await main.dashboard_delete_report("a" * 16)

        delete_record.assert_called_once_with("a" * 16)
        self.assertEqual(response.status_code, 303)
        self.assertEqual(response.headers["location"], "/reports")

    async def test_dashboard_route_renders_upload_page(self) -> None:
        with patch.object(main, "list_vector_files", return_value=[]):
            response = await main.dashboard_page()

        self.assertEqual(response.status_code, 200)
        body = response.body.decode("utf-8")
        self.assertIn('<article id="view-knowledge" class="view" data-view-panel="knowledge">', body)

    async def test_dashboard_post_uploads_knowledge_files_without_javascript(self) -> None:
        expected_uploads = [
            {"filename": "manual.docx", "file_id": "1783500000_manual.docx", "task_id": "task-1"},
            {"filename": "guide.pdf", "file_id": "1783500001_guide.pdf", "task_id": "task-2"},
        ]
        uploaded_names = []

        async def fake_upload_knowledge_file(upload):
            uploaded_names.append(upload.filename)
            return expected_uploads[len(uploaded_names) - 1]

        async def fake_list_vector_files():
            return [
                {
                    "file_id": "1783409846_existing.docx",
                    "display_name": "existing.docx",
                    "file_type": "docx",
                    "file_size": 120,
                    "upload_time": "2026-07-07T10:00:00",
                    "total_chunks": 3,
                }
            ]

        uploads = [
            main.UploadFile(filename="manual.docx", file=io.BytesIO(b"manual")),
            main.UploadFile(filename="guide.pdf", file=io.BytesIO(b"guide")),
        ]

        with (
            patch.object(main, "upload_knowledge_file", fake_upload_knowledge_file),
            patch.object(main, "list_vector_files", fake_list_vector_files),
        ):
            response = await main.dashboard_upload_knowledge_page(uploads)

        self.assertEqual(uploaded_names, ["manual.docx", "guide.pdf"])
        self.assertEqual(response.status_code, 200)
        body = response.body.decode("utf-8")
        self.assertIn("已提交 2 个文件到 RAG。", body)
        self.assertIn("manual.docx", body)
        self.assertIn("1783500000_manual.docx", body)
        self.assertIn("task-2", body)
        self.assertIn("existing.docx", body)


if __name__ == "__main__":
    unittest.main()
