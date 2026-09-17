from __future__ import annotations

import os
import unittest
from io import BytesIO
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException, UploadFile

os.environ.setdefault("SESSION_HMAC_SECRET", "session-secret-32-bytes-for-tests!!")
os.environ.setdefault("GATEWAY_BRIDGE_TOKEN", "gateway-token-32-bytes-for-tests!!!")

import knowledge_store


def runtime_configuration(vector_search_host: str = "http://vector:8900") -> dict[str, object]:
    return {
        "configured": True,
        "vector_search_host": vector_search_host,
        "rag_service_mcp_url": f"{vector_search_host}/mcp",
        "updated_at": "2026-09-16T12:00:00+00:00",
    }


def upload_file(filename: str, content: bytes) -> UploadFile:
    return UploadFile(file=BytesIO(content), filename=filename)


class KnowledgeUploadTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        patcher = patch.object(
            knowledge_store,
            "get_knowledge_configuration",
            return_value=runtime_configuration(),
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    async def test_upload_forwards_file_and_collection_to_runtime_vector_service(self) -> None:
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

            async def post(self, url, files, data):
                captured["url"] = url
                captured["data"] = data
                file_tuple = files["file"]
                captured["file_name"] = file_tuple[0]
                captured["file_media_type"] = file_tuple[2]
                captured["file_bytes"] = file_tuple[1].read()
                return FakeResponse()

        with (
            patch.object(knowledge_store.httpx, "AsyncClient", FakeClient),
        ):
            payload = await knowledge_store.upload_knowledge_file(
                upload_file("guide.md", b"# guide"),
                "team docs",
            )

        self.assertEqual(payload, {"filename": "guide.md", "file_id": "rag-file-1", "task_id": "task-123"})
        self.assertEqual(captured["url"], "http://vector:8900/api/v1/upload_file")
        self.assertEqual(captured["file_name"], "guide.md")
        self.assertEqual(captured["file_media_type"], "text/markdown")
        self.assertEqual(captured["file_bytes"], b"# guide")
        self.assertEqual(captured["data"], {"collection_name": "team docs"})

    async def test_upload_omits_collection_field_for_legacy_default_request(self) -> None:
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
                captured["filename"] = files["file"][0]
                return FakeResponse()

        with patch.object(knowledge_store.httpx, "AsyncClient", FakeClient):
            payload = await knowledge_store.upload_knowledge_file(upload_file("guide.md", b"# guide"))

        self.assertEqual(captured["url"], "http://vector:8900/api/v1/upload_file")
        self.assertEqual(captured["filename"], "guide.md")
        self.assertEqual(payload["task_id"], "task-123")

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
    def setUp(self) -> None:
        patcher = patch.object(
            knowledge_store,
            "get_knowledge_configuration",
            return_value=runtime_configuration(),
        )
        patcher.start()
        self.addCleanup(patcher.stop)

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

            async def get(self, url, params):
                captured["url"] = url
                captured["params"] = params
                return FakeResponse()

        with (
            patch.object(knowledge_store.httpx, "AsyncClient", FakeClient),
        ):
            payload = await knowledge_store.get_vector_task_status("task-123", "team docs")

        self.assertEqual(captured["url"], "http://vector:8900/api/v1/tasks/task-123")
        self.assertEqual(captured["params"], {"collection_name": "team docs"})
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

    async def test_get_vector_task_status_omits_collection_query_for_legacy_task(self) -> None:
        captured: dict[str, object] = {}

        class FakeResponse:
            status_code = 200

            def json(self):
                return {"task_id": "task-123", "status": "processing"}

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

        with patch.object(knowledge_store.httpx, "AsyncClient", FakeClient):
            payload = await knowledge_store.get_vector_task_status("task-123")

        self.assertEqual(captured["url"], "http://vector:8900/api/v1/tasks/task-123")
        self.assertEqual(payload["status"], "processing")


class VectorFileListTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        patcher = patch.object(
            knowledge_store,
            "get_knowledge_configuration",
            return_value=runtime_configuration("http://vector-host:8900"),
        )
        patcher.start()
        self.addCleanup(patcher.stop)

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
            patch.object(knowledge_store.httpx, "AsyncClient", FakeClient),
        ):
            files = await knowledge_store.list_vector_files("team docs/2026")

        self.assertEqual(
            captured["urls"],
            [
                "http://vector-host:6335/collections/team%20docs%2F2026/points/scroll",
                "http://vector-host:6335/collections/team%20docs%2F2026/points/scroll",
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
                    "collection_name": "team docs/2026",
                },
                {
                    "file_id": "1783409846_manual.docx",
                    "display_name": "manual.docx",
                    "file_type": "docx",
                    "file_size": 120,
                    "upload_time": "2026-07-06T09:35:51",
                    "total_chunks": 3,
                    "collection_name": "team docs/2026",
                },
            ],
        )

    async def test_list_vector_files_resolves_default_from_same_service_snapshot(self) -> None:
        captured: dict[str, object] = {}

        class FakeResponse:
            status_code = 200

            def json(self):
                return {"result": {"points": [], "next_page_offset": None}}

        class FakeClient:
            def __init__(self, *args, **kwargs):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, tb):
                return False

            async def post(self, url, json):
                captured["url"] = url
                return FakeResponse()

        default_collection = AsyncMock(return_value="primary-docs")
        with (
            patch.object(knowledge_store.httpx, "AsyncClient", FakeClient),
            patch.object(
                knowledge_store,
                "default_vector_collection_name",
                default_collection,
            ),
        ):
            self.assertEqual(await knowledge_store.list_vector_files(), [])

        default_collection.assert_awaited_once_with("http://vector-host:8900")
        self.assertEqual(
            captured["url"],
            "http://vector-host:6335/collections/primary-docs/points/scroll",
        )

    async def test_delete_vector_file_uses_vector_service_and_collection(self) -> None:
        captured: dict[str, object] = {}

        class FakeResponse:
            status_code = 200

            def json(self):
                return {"success": True, "deleted_chunks": 4, "message": "deleted"}

        class FakeClient:
            def __init__(self, *args, **kwargs):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, tb):
                return False

            async def delete(self, url, params):
                captured["url"] = url
                captured["params"] = params
                return FakeResponse()

        default_collection = AsyncMock(return_value="documents")
        with (
            patch.object(knowledge_store.httpx, "AsyncClient", FakeClient),
            patch.object(
                knowledge_store,
                "default_vector_collection_name",
                default_collection,
            ),
        ):
            payload = await knowledge_store.delete_vector_file(
                "1783409846_manual.docx",
                "team docs",
            )

        self.assertEqual(captured["url"], "http://vector-host:8900/api/v1/files/1783409846_manual.docx")
        self.assertEqual(captured["params"], {"collection_name": "team docs"})
        default_collection.assert_awaited_once_with("http://vector-host:8900")
        self.assertEqual(
            payload,
            {
                "file_id": "1783409846_manual.docx",
                "collection_name": "team docs",
                "deleted_chunks": 4,
                "message": "deleted",
            },
        )

    async def test_delete_vector_file_omits_collection_query_for_default_collection(self) -> None:
        captured: dict[str, object] = {}

        class FakeResponse:
            status_code = 200

            def json(self):
                return {"success": True, "deleted_chunks": 2, "message": "deleted"}

        class FakeClient:
            def __init__(self, *args, **kwargs):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, tb):
                return False

            async def delete(self, url):
                captured["url"] = url
                return FakeResponse()

        default_collection = AsyncMock(return_value="primary-docs")
        with (
            patch.object(knowledge_store.httpx, "AsyncClient", FakeClient),
            patch.object(
                knowledge_store,
                "default_vector_collection_name",
                default_collection,
            ),
        ):
            payload = await knowledge_store.delete_vector_file(
                "legacy-file.txt",
                "primary-docs",
            )

        self.assertEqual(captured["url"], "http://vector-host:8900/api/v1/files/legacy-file.txt")
        default_collection.assert_awaited_once_with("http://vector-host:8900")
        self.assertEqual(payload["collection_name"], "primary-docs")
        self.assertEqual(payload["deleted_chunks"], 2)

    async def test_delete_vector_file_requires_file_id(self) -> None:
        with self.assertRaises(HTTPException) as raised:
            await knowledge_store.delete_vector_file(" ")

        self.assertEqual(raised.exception.status_code, 400)

    async def test_default_collection_name_uses_service_metadata(self) -> None:
        collections = [
            {
                "name": "primary-docs",
                "points_count": 2,
                "vectors_count": 2,
                "status": "green",
                "schema": "legacy",
                "is_default": True,
            },
            {
                "name": "project-a",
                "points_count": 1,
                "vectors_count": 1,
                "status": "green",
                "schema": "legacy",
                "is_default": False,
            },
        ]

        with patch.object(
            knowledge_store,
            "list_vector_collections",
            AsyncMock(return_value=collections),
        ):
            self.assertEqual(
                await knowledge_store.default_vector_collection_name(),
                "primary-docs",
            )


class VectorCollectionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        patcher = patch.object(
            knowledge_store,
            "get_knowledge_configuration",
            return_value=runtime_configuration(),
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    async def test_list_vector_collections_validates_and_returns_contract(self) -> None:
        captured: dict[str, object] = {}

        class FakeResponse:
            status_code = 200
            text = ""

            def json(self):
                return {
                    "success": True,
                    "collections": [
                        {
                            "name": "documents",
                            "points_count": 12,
                            "vectors_count": 12,
                            "status": "green",
                            "schema": "hybrid",
                            "is_default": True,
                        }
                    ],
                    "total": 1,
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

        with patch.object(knowledge_store.httpx, "AsyncClient", FakeClient):
            payload = await knowledge_store.list_vector_collections()

        self.assertEqual(captured["url"], "http://vector:8900/api/v1/collections")
        self.assertEqual(
            payload,
            [
                {
                    "name": "documents",
                    "points_count": 12,
                    "vectors_count": 12,
                    "status": "green",
                    "schema": "hybrid",
                    "is_default": True,
                }
            ],
        )

    async def test_list_vector_collections_rejects_invalid_item(self) -> None:
        class FakeResponse:
            status_code = 200
            text = ""

            def json(self):
                return {
                    "success": True,
                    "collections": [{"name": "documents", "points_count": "12"}],
                    "total": 1,
                }

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
                await knowledge_store.list_vector_collections()

        self.assertEqual(raised.exception.status_code, 502)

    async def test_create_vector_collection_forwards_normalized_name(self) -> None:
        captured: dict[str, object] = {}

        class FakeResponse:
            status_code = 200
            text = ""

            def json(self):
                return {
                    "success": True,
                    "collection_name": "project-a",
                    "message": "created",
                }

        class FakeClient:
            def __init__(self, *args, **kwargs):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, tb):
                return False

            async def post(self, url, json):
                captured["url"] = url
                captured["json"] = json
                return FakeResponse()

        with patch.object(knowledge_store.httpx, "AsyncClient", FakeClient):
            payload = await knowledge_store.create_vector_collection(" project-a ")

        self.assertEqual(captured["url"], "http://vector:8900/api/v1/collections")
        self.assertEqual(captured["json"], {"collection_name": "project-a"})
        self.assertEqual(payload, {"collection_name": "project-a", "message": "created"})

    def test_collection_name_is_required_and_limited_to_128_characters(self) -> None:
        for collection_name in (" ", "x" * 129):
            with self.subTest(collection_name=collection_name):
                with self.assertRaises(HTTPException) as raised:
                    knowledge_store.validate_collection_name(collection_name)
                self.assertEqual(raised.exception.status_code, 400)


if __name__ == "__main__":
    unittest.main()
