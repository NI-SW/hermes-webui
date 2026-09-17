from __future__ import annotations

import os
import unittest
from io import BytesIO
from pathlib import Path
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException, UploadFile
from fastapi.routing import APIRoute
from pydantic import ValidationError

os.environ.setdefault("SESSION_HMAC_SECRET", "session-secret-32-bytes-for-tests!!")
os.environ.setdefault("GATEWAY_BRIDGE_TOKEN", "gateway-token-32-bytes-for-tests!!!")
os.environ.setdefault("I2STREAM_INSTALL_INTERNAL_TOKEN", "install-token-at-least-32-bytes-long")

import db
import knowledge_store
import main
from knowledge_config_store import (
    empty_knowledge_configuration,
    get_knowledge_configuration,
    save_knowledge_configuration,
)
from models import KnowledgeCollectionCreateRequest, KnowledgeServiceConfigurationRequest


def upload_file(filename: str, content: bytes) -> UploadFile:
    return UploadFile(file=BytesIO(content), filename=filename)


class KnowledgeConfigurationValidationTests(unittest.TestCase):
    def test_normalizes_valid_rest_and_mcp_urls(self) -> None:
        request = KnowledgeServiceConfigurationRequest(
            vector_search_host="https://rag.example.test:8900/",
            rag_service_mcp_url="https://rag.example.test:8900/mcp/",
        )

        self.assertEqual(request.vector_search_host, "https://rag.example.test:8900")
        self.assertEqual(request.rag_service_mcp_url, "https://rag.example.test:8900/mcp")

    def test_rejects_unsafe_or_ambiguous_urls(self) -> None:
        invalid_pairs = (
            ("rag.example.test:8900", "http://rag.example.test:8900/mcp"),
            ("http://rag.example.test", "http://rag.example.test:8900/mcp"),
            ("http://user:pass@rag.example.test:8900", "http://rag.example.test:8900/mcp"),
            ("http://rag.example.test:8900/api", "http://rag.example.test:8900/mcp"),
            ("http://rag.example.test:8900?debug=1", "http://rag.example.test:8900/mcp"),
            ("http://rag.example.test:8900", "http://rag.example.test:8900"),
            ("http://rag.example.test:8900", "http://rag.example.test:8900/mcp?debug=1"),
            ("http://rag.example.test:8900", "ftp://rag.example.test:8900/mcp"),
        )

        for vector_url, mcp_url in invalid_pairs:
            with self.subTest(vector_url=vector_url, mcp_url=mcp_url):
                with self.assertRaises(ValidationError):
                    KnowledgeServiceConfigurationRequest(
                        vector_search_host=vector_url,
                        rag_service_mcp_url=mcp_url,
                    )

    def test_collection_create_request_trims_name_and_rejects_extra_fields(self) -> None:
        request = KnowledgeCollectionCreateRequest(collection_name=" project-a ")
        self.assertEqual(request.collection_name, "project-a")

        for payload in (
            {"collection_name": "   "},
            {"collection_name": "x" * 129},
            {"collection_name": "project-a", "unexpected": True},
        ):
            with self.subTest(payload=payload):
                with self.assertRaises(ValidationError):
                    KnowledgeCollectionCreateRequest.model_validate(payload)


class KnowledgeConfigurationPersistenceTests(unittest.TestCase):
    def test_empty_then_upserted_single_row_configuration(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "chat.db"
            with (
                patch.object(db, "SQLITE_DB_PATH", db_path),
                patch.object(db, "chat_schema_ready", False),
            ):
                self.assertEqual(
                    empty_knowledge_configuration(),
                    {
                        "configured": False,
                        "vector_search_host": "",
                        "rag_service_mcp_url": "",
                        "updated_at": None,
                    },
                )
                self.assertIsNone(get_knowledge_configuration())

                first = save_knowledge_configuration(
                    "http://rag-one.test:8900",
                    "http://rag-one.test:8900/mcp",
                )
                second = save_knowledge_configuration(
                    "https://rag-two.test:9443",
                    "https://rag-two.test:9443/mcp",
                )
                loaded = get_knowledge_configuration()

            self.assertTrue(first["configured"])
            self.assertEqual(second, loaded)
            self.assertEqual(second["vector_search_host"], "https://rag-two.test:9443")
            self.assertEqual(second["rag_service_mcp_url"], "https://rag-two.test:9443/mcp")
            self.assertIsInstance(second["updated_at"], str)


class KnowledgeRuntimeConfigurationTests(unittest.IsolatedAsyncioTestCase):
    async def test_all_knowledge_operations_return_503_when_not_configured(self) -> None:
        with patch.object(knowledge_store, "get_knowledge_configuration", return_value=None):
            operations = (
                lambda: knowledge_store.upload_knowledge_file(upload_file("guide.md", b"guide")),
                lambda: knowledge_store.get_vector_task_status("task-1"),
                knowledge_store.list_vector_files,
                lambda: knowledge_store.delete_vector_file("file-1"),
                knowledge_store.list_vector_collections,
                lambda: knowledge_store.create_vector_collection("project-a"),
            )
            for operation in operations:
                with self.subTest(operation=operation):
                    with self.assertRaises(HTTPException) as raised:
                        await operation()
                    self.assertEqual(raised.exception.status_code, 503)
                    self.assertEqual(
                        raised.exception.detail,
                        "Knowledge service is not configured",
                    )

    async def test_connection_check_probes_health_and_mcp_endpoint(self) -> None:
        captured: list[tuple[str, str]] = []

        class FakeResponse:
            status_code = 200
            text = "ok"

        class FakeClient:
            def __init__(self, *args, **kwargs):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, tb):
                return False

            async def get(self, url, **kwargs):
                captured.append(("GET", url))
                return FakeResponse()

            async def head(self, url, **kwargs):
                captured.append(("HEAD", url))
                return FakeResponse()

        request = KnowledgeServiceConfigurationRequest(
            vector_search_host="http://rag.test:8900",
            rag_service_mcp_url="http://rag.test:8900/mcp",
        )
        with patch.object(knowledge_store.httpx, "AsyncClient", FakeClient):
            checks = await knowledge_store.check_knowledge_service_connections(request)

        self.assertEqual(
            captured,
            [
                ("GET", "http://rag.test:8900/health"),
                ("HEAD", "http://rag.test:8900/mcp"),
            ],
        )
        self.assertEqual(
            checks,
            {
                "vector_search": {
                    "status": "reachable",
                    "url": "http://rag.test:8900/health",
                },
                "rag_service_mcp": {
                    "status": "reachable",
                    "url": "http://rag.test:8900/mcp",
                },
            },
        )

    async def test_connection_check_reports_which_service_failed(self) -> None:
        class FakeResponse:
            status_code = 503
            text = "unavailable"

        class FakeClient:
            def __init__(self, *args, **kwargs):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, tb):
                return False

            async def get(self, url, **kwargs):
                return FakeResponse()

            async def head(self, url, **kwargs):
                return FakeResponse()

        request = KnowledgeServiceConfigurationRequest(
            vector_search_host="http://rag.test:8900",
            rag_service_mcp_url="http://rag.test:8900/mcp",
        )
        with patch.object(knowledge_store.httpx, "AsyncClient", FakeClient):
            with self.assertRaises(HTTPException) as raised:
                await knowledge_store.check_knowledge_service_connections(request)

        self.assertEqual(raised.exception.status_code, 502)
        self.assertIn("Vector service health check returned HTTP 503", raised.exception.detail)


class KnowledgeConfigurationApiTests(unittest.IsolatedAsyncioTestCase):
    async def test_get_returns_complete_empty_configuration(self) -> None:
        with patch.object(main, "get_knowledge_configuration", return_value=None):
            response = await main.api_get_knowledge_configuration()

        self.assertEqual(
            response,
            {
                "code": 0,
                "status": "success",
                "configuration": {
                    "configured": False,
                    "vector_search_host": "",
                    "rag_service_mcp_url": "",
                    "updated_at": None,
                },
            },
        )

    async def test_put_persists_normalized_urls_and_requires_mcp_reload(self) -> None:
        request = KnowledgeServiceConfigurationRequest(
            vector_search_host="http://rag.test:8900/",
            rag_service_mcp_url="http://rag.test:8900/mcp/",
        )
        saved = {
            "configured": True,
            "vector_search_host": "http://rag.test:8900",
            "rag_service_mcp_url": "http://rag.test:8900/mcp",
            "updated_at": "2026-09-16T12:00:00+00:00",
        }
        with patch.object(main, "save_knowledge_configuration", return_value=saved) as save:
            response = await main.api_put_knowledge_configuration(request)

        save.assert_called_once_with(
            "http://rag.test:8900",
            "http://rag.test:8900/mcp",
        )
        self.assertEqual(
            response,
            {
                "code": 0,
                "status": "success",
                "configuration": saved,
                "mcp_reload_required": True,
            },
        )

    async def test_check_does_not_persist_configuration(self) -> None:
        request = KnowledgeServiceConfigurationRequest(
            vector_search_host="http://rag.test:8900",
            rag_service_mcp_url="http://rag.test:8900/mcp",
        )
        checks = {
            "vector_search": {
                "status": "reachable",
                "url": "http://rag.test:8900/health",
            },
            "rag_service_mcp": {
                "status": "reachable",
                "url": "http://rag.test:8900/mcp",
            },
        }
        check = AsyncMock(return_value=checks)
        with (
            patch.object(main, "check_knowledge_service_connections", check),
            patch.object(main, "save_knowledge_configuration") as save,
        ):
            response = await main.api_check_knowledge_configuration(request)

        check.assert_awaited_once_with(request)
        save.assert_not_called()
        self.assertEqual(response["configuration"]["configured"], True)
        self.assertEqual(response["configuration"]["updated_at"], None)
        self.assertEqual(response["checks"], checks)

    def test_all_configuration_routes_require_install_auth(self) -> None:
        expected = {
            ("GET", "/api/knowledge/config"),
            ("PUT", "/api/knowledge/config"),
            ("POST", "/api/knowledge/config/check"),
            ("GET", "/api/knowledge/collections"),
            ("POST", "/api/knowledge/collections"),
        }
        protected: set[tuple[str, str]] = set()
        for route in main.app.routes:
            if not isinstance(route, APIRoute):
                continue
            if route.path not in {path for _, path in expected}:
                continue
            dependencies = {dependency.call for dependency in route.dependant.dependencies}
            if main.require_install_auth not in dependencies:
                continue
            protected.update((method, route.path) for method in route.methods & {"GET", "PUT", "POST"})

        self.assertEqual(protected, expected)


if __name__ == "__main__":
    unittest.main()
