from __future__ import annotations

import ast
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
BACKEND_ROOT = REPO_ROOT / "i2stream_backend"


def _registered_backend_routes() -> set[str]:
    tree = ast.parse((BACKEND_ROOT / "main.py").read_text(encoding="utf-8"))
    routes: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for decorator in node.decorator_list:
            if not isinstance(decorator, ast.Call) or not isinstance(decorator.func, ast.Attribute):
                continue
            if not isinstance(decorator.func.value, ast.Name) or decorator.func.value.id != "app":
                continue
            if not decorator.args:
                continue
            route = decorator.args[0]
            if isinstance(route, ast.Constant) and isinstance(route.value, str):
                routes.add(route.value)
    return routes


def test_webui_facade_upstreams_exist_in_bundled_backend() -> None:
    routes = _registered_backend_routes()

    assert {
        "/api/knowledge/files",
        "/api/knowledge/files/{file_id}",
        "/api/knowledge/tasks/{task_id}",
        "/api/reports",
        "/api/reports/{token}",
        "/api/reports/{token}/content",
        "/api/chat-conversations",
        "/api/conversations/{conversation_id}/messages",
    } <= routes


def test_extension_and_gateway_compatibility_routes_remain_available() -> None:
    routes = _registered_backend_routes()

    assert {
        "/internal/gateway",
        "/api/agent/requests",
        "/api/agent/requests/{request_id}/progress",
        "/api/chat-clients",
        "/api/conversations/{conversation_id}/messages/{message_id}/feedback",
    } <= routes
