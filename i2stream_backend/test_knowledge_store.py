from __future__ import annotations

import os
import unittest
from io import BytesIO
from unittest.mock import patch

from fastapi import HTTPException, UploadFile

os.environ.setdefault("VECTOR_SEARCH_HOST", "http://127.0.0.1:8900")
os.environ.setdefault("SESSION_HMAC_SECRET", "session-secret-32-bytes-for-tests!!")
os.environ.setdefault("GATEWAY_BRIDGE_TOKEN", "gateway-token-32-bytes-for-tests!!!")

import knowledge_store


def upload_file(filename: str, content: bytes) -> UploadFile:
    return UploadFile(file=BytesIO(content), filename=filename)


class KnowledgeUploadTests(unittest.IsolatedAsyncioTestCase):
    async def test_upload_forwards_file_to_vector_without_metadata(self) -> None:
        captured: dict[str, object] = {}

        class FakeResponse:
            status_code = 200

            def json(self):
                return {"file_id": "rag-file-1", "task_id": "task-123"}

        class FakeClient:
            def __init__(self, *args, **kwargs):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, tb):
                return False

            async def post(self, url, files):
                captured["url"] = url
                captured["has_data_argument"] = False
                file_tuple = files["file"]
                captured["file_name"] = file_tuple[0]
                captured["file_media_type"] = file_tuple[2]
                captured["file_bytes"] = file_tuple[1].read()
                return FakeResponse()

        with (
            patch.object(knowledge_store.settings, "vector_search_host", "http://vector:8900"),
            patch.object(knowledge_store.httpx, "AsyncClient", FakeClient),
        ):
            payload = await knowledge_store.upload_knowledge_file(upload_file("guide.md", b"# guide"))

        self.assertEqual(payload, {"filename": "guide.md", "file_id": "rag-file-1", "task_id": "task-123"})
        self.assertEqual(captured["url"], "http://vector:8900/api/v1/upload_file")
        self.assertEqual(captured["file_name"], "guide.md")
        self.assertEqual(captured["file_media_type"], "text/markdown")
        self.assertEqual(captured["file_bytes"], b"# guide")
        self.assertFalse(captured["has_data_argument"])

    async def test_unsupported_extension_is_rejected_before_vector_request(self) -> None:
        with self.assertRaises(HTTPException) as raised:
            await knowledge_store.upload_knowledge_file(upload_file("source.exe", b"binary"))

        self.assertEqual(raised.exception.status_code, 400)

    async def test_empty_file_is_rejected_before_vector_request(self) -> None:
        with self.assertRaises(HTTPException) as raised:
            await knowledge_store.upload_knowledge_file(upload_file("empty.txt", b""))

        self.assertEqual(raised.exception.status_code, 400)

    async def test_vector_response_must_include_file_id_and_task_id(self) -> None:
        class FakeResponse:
            status_code = 200

            def json(self):
                return {"task_id": "task-123"}

        class FakeClient:
            def __init__(self, *args, **kwargs):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, tb):
                return False

            async def post(self, url, files):
                return FakeResponse()

        with patch.object(knowledge_store.httpx, "AsyncClient", FakeClient):
            with self.assertRaises(HTTPException) as raised:
                await knowledge_store.upload_knowledge_file(upload_file("guide.md", b"# guide"))

        self.assertEqual(raised.exception.status_code, 502)


class KnowledgeTaskStatusTests(unittest.IsolatedAsyncioTestCase):
    async def test_get_vector_task_status_marks_completed_as_terminal(self) -> None:
        captured: dict[str, object] = {}

        class FakeResponse:
            status_code = 200

            def json(self):
                return {
                    "task_id": "task-123",
                    "status": "completed",
                    "file_id": "rag-file-1",
                    "file_name": "guide.md",
                    "chunks_count": 3,
                    "message": "done",
                }

        class FakeClient:
            def __init__(self, *args, **kwargs):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, tb):
                return False

            async def get(self, url):
                captured["url"] = url
                return FakeResponse()

        with (
            patch.object(knowledge_store.settings, "vector_search_host", "http://vector:8900"),
            patch.object(knowledge_store.httpx, "AsyncClient", FakeClient),
        ):
            payload = await knowledge_store.get_vector_task_status("task-123")

        self.assertEqual(captured["url"], "http://vector:8900/api/v1/tasks/task-123")
        self.assertEqual(
            payload,
            {
                "task_id": "task-123",
                "status": "completed",
                "file_id": "rag-file-1",
                "file_name": "guide.md",
                "chunks_count": 3,
                "message": "done",
                "error": None,
                "terminal": True,
            },
        )

    async def test_get_vector_task_status_requires_status(self) -> None:
        class FakeResponse:
            status_code = 200

            def json(self):
                return {"task_id": "task-123"}

        class FakeClient:
            def __init__(self, *args, **kwargs):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, tb):
                return False

            async def get(self, url):
                return FakeResponse()

        with patch.object(knowledge_store.httpx, "AsyncClient", FakeClient):
            with self.assertRaises(HTTPException) as raised:
                await knowledge_store.get_vector_task_status("task-123")

        self.assertEqual(raised.exception.status_code, 502)


class VectorFileListTests(unittest.IsolatedAsyncioTestCase):
    def test_qdrant_base_url_is_derived_from_vector_search_host(self) -> None:
        self.assertEqual(
            knowledge_store.qdrant_base_url_from_vector_host("http://192.168.34.65:8900"),
            "http://192.168.34.65:6335",
        )

    def test_display_name_is_derived_from_file_id(self) -> None:
        self.assertEqual(
            knowledge_store.display_name_from_file_id("1783409846_i2Stream 9.1.4 Beta软件配置手册.docx"),
            "i2Stream 9.1.4 Beta软件配置手册.docx",
        )
        self.assertEqual(knowledge_store.display_name_from_file_id("manual.docx"), "manual.docx")

    async def test_list_vector_files_scrolls_qdrant_and_deduplicates_file_id(self) -> None:
        captured: dict[str, object] = {"urls": [], "payloads": []}

        pages = [
            {
                "result": {
                    "points": [
                        {
                            "payload": {
                                "file_id": "1783409846_manual.docx",
                                "file_type": "docx",
                                "file_size": 120,
                                "upload_time": "2026-07-06T09:35:51",
                                "total_chunks": 3,
                                "is_deleted": "false",
                            }
                        },
                        {
                            "payload": {
                                "file_id": "1783409846_manual.docx",
                                "file_type": "docx",
                                "file_size": 120,
                                "upload_time": "2026-07-06T09:35:51",
                                "total_chunks": 3,
                                "is_deleted": "false",
                            }
                        },
                    ],
                    "next_page_offset": "next-page",
                }
            },
            {
                "result": {
                    "points": [
                        {
                            "payload": {
                                "file_id": "1783409847_guide.xlsx",
                                "file_type": "xlsx",
                                "file_size": 240,
                                "upload_time": "2026-07-07T10:00:00",
                                "total_chunks": 5,
                                "is_deleted": "false",
                            }
                        }
                    ],
                    "next_page_offset": None,
                }
            },
        ]

        class FakeResponse:
            status_code = 200

            def __init__(self, payload):
                self._payload = payload

            def json(self):
                return self._payload

        class FakeClient:
            def __init__(self, *args, **kwargs):
                self.calls = 0

            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, tb):
                return False

            async def post(self, url, json):
                captured["urls"].append(url)
                captured["payloads"].append(json)
                payload = pages[self.calls]
                self.calls += 1
                return FakeResponse(payload)

        with (
            patch.object(knowledge_store.settings, "vector_search_host", "http://vector-host:8900"),
            patch.object(knowledge_store.httpx, "AsyncClient", FakeClient),
        ):
            files = await knowledge_store.list_vector_files()

        self.assertEqual(
            captured["urls"],
            [
                "http://vector-host:6335/collections/documents/points/scroll",
                "http://vector-host:6335/collections/documents/points/scroll",
            ],
        )
        self.assertEqual(captured["payloads"][0]["filter"]["must"][0]["key"], "is_deleted")
        self.assertEqual(captured["payloads"][1]["offset"], "next-page")
        self.assertEqual(
            files,
            [
                {
                    "file_id": "1783409847_guide.xlsx",
                    "display_name": "guide.xlsx",
                    "file_type": "xlsx",
                    "file_size": 240,
                    "upload_time": "2026-07-07T10:00:00",
                    "total_chunks": 5,
                },
                {
                    "file_id": "1783409846_manual.docx",
                    "display_name": "manual.docx",
                    "file_type": "docx",
                    "file_size": 120,
                    "upload_time": "2026-07-06T09:35:51",
                    "total_chunks": 3,
                },
            ],
        )

    async def test_delete_vector_file_removes_qdrant_points_by_file_id(self) -> None:
        captured: dict[str, object] = {}

        class FakeResponse:
            status_code = 200

            def json(self):
                return {"status": "acknowledged", "result": {"operation_id": 42}}

        class FakeClient:
            def __init__(self, *args, **kwargs):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, tb):
                return False

            async def post(self, url, json, params):
                captured["url"] = url
                captured["payload"] = json
                captured["params"] = params
                return FakeResponse()

        with (
            patch.object(knowledge_store.settings, "vector_search_host", "http://vector-host:8900"),
            patch.object(knowledge_store.httpx, "AsyncClient", FakeClient),
        ):
            payload = await knowledge_store.delete_vector_file("1783409846_manual.docx")

        self.assertEqual(captured["url"], "http://vector-host:6335/collections/documents/points/delete")
        self.assertEqual(captured["params"], {"wait": "true"})
        self.assertEqual(
            captured["payload"],
            {
                "filter": {
                    "must": [
                        {
                            "key": "file_id",
                            "match": {"value": "1783409846_manual.docx"},
                        }
                    ]
                }
            },
        )
        self.assertEqual(payload, {"file_id": "1783409846_manual.docx", "operation_id": 42})

    async def test_delete_vector_file_requires_file_id(self) -> None:
        with self.assertRaises(HTTPException) as raised:
            await knowledge_store.delete_vector_file(" ")

        self.assertEqual(raised.exception.status_code, 400)


if __name__ == "__main__":
    unittest.main()
