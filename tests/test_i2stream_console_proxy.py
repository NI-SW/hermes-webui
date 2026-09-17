"""Behavioral tests for the narrow i2Stream console compatibility proxy."""

from __future__ import annotations

import io
import json
from types import SimpleNamespace
from urllib.error import URLError
from urllib.parse import urlparse
from urllib.request import HTTPRedirectHandler

import pytest


class FakeHandler:
    def __init__(self, body: bytes = b"", headers: dict[str, str] | None = None):
        self.status = None
        self.headers = headers or {}
        self.sent_headers: list[tuple[str, str]] = []
        self.rfile = io.BytesIO(body)
        self.wfile = io.BytesIO()
        self.close_connection = False

    def send_response(self, status: int) -> None:
        self.status = status

    def send_header(self, name: str, value: str) -> None:
        self.sent_headers.append((name, value))

    def end_headers(self) -> None:
        pass

    def header(self, name: str) -> str | None:
        for key, value in reversed(self.sent_headers):
            if key.lower() == name.lower():
                return value
        return None


class FakeResponse:
    def __init__(
        self,
        body: bytes,
        *,
        status: int = 200,
        headers: dict[str, str] | None = None,
        forbid_unbounded_read: bool = False,
    ):
        self.status = status
        self.headers = headers or {"Content-Type": "application/json"}
        self._stream = io.BytesIO(body)
        self.forbid_unbounded_read = forbid_unbounded_read
        self.read_sizes: list[int] = []
        self.closed = False

    def read(self, size: int = -1) -> bytes:
        if self.forbid_unbounded_read and size < 0:
            raise AssertionError("streaming response was buffered with an unbounded read")
        self.read_sizes.append(size)
        return self._stream.read(size)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        self.closed = True
        return False


class FakeOpener:
    def __init__(self, response: FakeResponse):
        self.response = response
        self.requests = []

    def open(self, request, timeout: float):
        self.requests.append((request, timeout))
        return self.response


def _json_body(handler: FakeHandler) -> dict:
    return json.loads(handler.wfile.getvalue().decode("utf-8"))


@pytest.mark.parametrize(
    ("method", "webui_path", "upstream_path"),
    [
        ("GET", "/api/i2stream-console/knowledge/config", "/api/knowledge/config"),
        ("PUT", "/api/i2stream-console/knowledge/config", "/api/knowledge/config"),
        (
            "POST",
            "/api/i2stream-console/knowledge/config/check",
            "/api/knowledge/config/check",
        ),
        ("GET", "/api/i2stream-console/knowledge/collections", "/api/knowledge/collections"),
        ("POST", "/api/i2stream-console/knowledge/collections", "/api/knowledge/collections"),
        ("GET", "/api/i2stream-console/knowledge/files", "/api/knowledge/files"),
        ("POST", "/api/i2stream-console/knowledge/files", "/api/knowledge/files"),
        (
            "DELETE",
            "/api/i2stream-console/knowledge/files/manual%20v2.pdf",
            "/api/knowledge/files/manual%20v2.pdf",
        ),
        ("GET", "/api/i2stream-console/knowledge/tasks/task_123", "/api/knowledge/tasks/task_123"),
        ("GET", "/api/i2stream-console/reports", "/api/reports"),
        ("GET", "/api/i2stream-console/nodes", "/api/nodes"),
        (
            "POST",
            "/api/i2stream-console/logmonitor/preflight",
            "/api/logmonitor/preflight",
        ),
        (
            "POST",
            "/api/i2stream-console/logmonitor/installations",
            "/api/logmonitor/installations",
        ),
        (
            "GET",
            "/api/i2stream-console/logmonitor/installations/0123456789abcdef0123456789abcdef",
            "/api/logmonitor/installations/0123456789abcdef0123456789abcdef",
        ),
        (
            "DELETE",
            "/api/i2stream-console/nodes/2001%3Adb8%3A%3A1",
            "/api/nodes/2001%3Adb8%3A%3A1",
        ),
        ("DELETE", "/api/i2stream-console/reports/abcdefghijklmnop", "/api/reports/abcdefghijklmnop"),
        (
            "GET",
            "/api/i2stream-console/reports/abcdefghijklmnop/content",
            "/api/reports/abcdefghijklmnop/content",
        ),
        (
            "GET",
            "/api/i2stream-console/conversations?client_id=abcdefghijklmnop",
            "/api/chat-conversations",
        ),
        (
            "GET",
            "/api/i2stream-console/conversations/conversation_1/messages?client_id=abcdefghijklmnop",
            "/api/conversations/conversation_1/messages",
        ),
    ],
)
def test_allowlist_maps_only_console_data_routes(method, webui_path, upstream_path):
    from api.i2stream_console import resolve_proxy_target

    target = resolve_proxy_target(method, urlparse(webui_path))
    assert target.upstream_path == upstream_path


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("GET", "/api/i2stream-console/internal/gateway"),
        ("POST", "/api/i2stream-console/agent/requests"),
        ("GET", "/api/i2stream-console/generated-files/token/content"),
        ("POST", "/api/i2stream-console/reports"),
        ("POST", "/api/i2stream-console/nodes"),
        ("POST", "/api/i2stream-console/heartbeat"),
        ("GET", "/api/i2stream-console/nodes?debug=1"),
        ("DELETE", "/api/i2stream-console/nodes/not-an-ip"),
        ("DELETE", "/api/i2stream-console/nodes/fe80%3A%3A1%25eth0"),
        ("PATCH", "/api/i2stream-console/knowledge/files"),
        ("GET", "/api/i2stream-console/reports/abc/content"),
        ("GET", "/api/i2stream-console/knowledge/files/a%2Fb"),
        ("GET", "/api/i2stream-console/logmonitor/preflight"),
        ("POST", "/api/i2stream-console/knowledge/config"),
        ("GET", "/api/i2stream-console/knowledge/config/check"),
        ("POST", "/api/i2stream-console/logmonitor/installations/not-a-job"),
        ("GET", "/api/i2stream-console/logmonitor/installations/not-a-job"),
    ],
)
def test_allowlist_rejects_protocol_and_malformed_routes(method, path):
    from api.i2stream_console import ProxyRouteError, resolve_proxy_target

    with pytest.raises(ProxyRouteError):
        resolve_proxy_target(method, urlparse(path))


def test_history_listing_requires_client_id_and_forwards_it_upstream():
    from api.i2stream_console import resolve_proxy_target

    listing = resolve_proxy_target(
        "GET",
        urlparse(
            "/api/i2stream-console/conversations?client_id=abcdefghijklmnop"
            "&limit=25&before=9001"
        ),
    )
    assert listing.upstream_query == "client_id=abcdefghijklmnop&limit=25&before=9001"
    assert listing.extra_headers == {}

    detail = resolve_proxy_target(
        "GET",
        urlparse(
            "/api/i2stream-console/conversations/conversation_1/messages"
            "?client_id=abcdefghijklmnop"
        ),
    )
    assert detail.upstream_query == ""
    assert detail.extra_headers == {"X-I2H-Client-Id": "abcdefghijklmnop"}


@pytest.mark.parametrize(
    ("method", "path", "upstream_path"),
    [
        (
            "GET",
            "/api/i2stream-console/knowledge/files?collection_name=team%20docs",
            "/api/knowledge/files",
        ),
        (
            "DELETE",
            "/api/i2stream-console/knowledge/files/manual.pdf?collection_name=team%20docs",
            "/api/knowledge/files/manual.pdf",
        ),
        (
            "GET",
            "/api/i2stream-console/knowledge/tasks/task-1?collection_name=team%20docs",
            "/api/knowledge/tasks/task-1",
        ),
    ],
)
def test_knowledge_routes_forward_one_canonical_collection_query(method, path, upstream_path):
    from api.i2stream_console import resolve_proxy_target

    target = resolve_proxy_target(method, urlparse(path))

    assert target.upstream_path == upstream_path
    assert target.upstream_query == "collection_name=team+docs"


@pytest.mark.parametrize(
    "path",
    [
        "/api/i2stream-console/knowledge/files?collection_name=",
        "/api/i2stream-console/knowledge/files?collection_name=docs&collection_name=other",
        "/api/i2stream-console/knowledge/files?debug=1",
        "/api/i2stream-console/knowledge/files?collection_name=%20docs",
        "/api/i2stream-console/knowledge/files?collection_name=" + ("a" * 129),
    ],
)
def test_knowledge_file_listing_requires_one_valid_collection(path):
    from api.i2stream_console import ProxyRouteError, resolve_proxy_target

    with pytest.raises(ProxyRouteError):
        resolve_proxy_target("GET", urlparse(path))


def test_knowledge_collection_routes_are_privileged():
    from api.i2stream_console import resolve_proxy_target

    for method in ("GET", "POST"):
        target = resolve_proxy_target(
            method,
            urlparse("/api/i2stream-console/knowledge/collections"),
        )
        assert target.privileged_route is True


@pytest.mark.parametrize(
    "url",
    [
        "/api/i2stream-console/conversations",
        "/api/i2stream-console/conversations?client_id=short",
        "/api/i2stream-console/conversations?client_id=abcdefghijklmnop&client_id=qrstuvwxyzabcdef",
        "/api/i2stream-console/conversations?limit=0",
        "/api/i2stream-console/conversations?limit=101",
        "/api/i2stream-console/conversations?before=a&before=b",
        "/api/i2stream-console/conversations?malformed",
        "/api/i2stream-console/conversations/c1/messages",
        "/api/i2stream-console/conversations/c1/messages?client_id=short",
        "/api/i2stream-console/conversations/c1/messages?client_id=abcdefghijklmnop&debug=1",
    ],
)
def test_history_query_rejects_unknown_duplicate_or_invalid_values(url):
    from api.i2stream_console import ProxyRouteError, resolve_proxy_target

    with pytest.raises(ProxyRouteError):
        resolve_proxy_target("GET", urlparse(url))


def test_proxy_strips_browser_authority_and_injects_configured_bearer(monkeypatch):
    from api import i2stream_console

    response = FakeResponse(b'{"files":[]}')
    opener = FakeOpener(response)
    monkeypatch.setattr(i2stream_console, "_upstream_opener", lambda _origin: opener)
    monkeypatch.setattr(i2stream_console, "I2STREAM_CONSOLE_BASE_URL", "http://127.0.0.1:50091")
    monkeypatch.setattr(i2stream_console, "I2STREAM_CONSOLE_BEARER_TOKEN", "internal-secret")

    handler = FakeHandler(
        headers={
            "Accept": "application/json",
            "Authorization": "Bearer browser-secret",
            "Cookie": "webui=session",
            "Origin": "https://webui.example",
            "X-I2H-Client-Id": "forged-client",
            "X-Hermes-CSRF-Token": "csrf",
        }
    )
    assert i2stream_console.handle_proxy(
        handler,
        urlparse("/api/i2stream-console/reports"),
        "GET",
    ) is True

    request, timeout = opener.requests[0]
    sent = {key.lower(): value for key, value in request.header_items()}
    assert request.full_url == "http://127.0.0.1:50091/api/reports"
    assert timeout == i2stream_console.I2STREAM_CONSOLE_TIMEOUT_SECONDS
    assert sent == {
        "accept": "application/json",
        "authorization": "Bearer internal-secret",
    }
    assert handler.status == 200
    assert _json_body(handler) == {"files": []}


def test_install_facade_requires_webui_auth_internal_token_and_ipv4_host(monkeypatch):
    from api import auth, i2stream_console

    target = i2stream_console.resolve_proxy_target(
        "POST",
        urlparse("/api/i2stream-console/logmonitor/preflight"),
    )
    handler = FakeHandler(headers={"Host": "192.168.34.65:8787"})

    monkeypatch.setattr(auth, "is_auth_enabled", lambda: False)
    monkeypatch.setattr(i2stream_console, "I2STREAM_CONSOLE_BEARER_TOKEN", "x" * 32)
    monkeypatch.setattr(i2stream_console, "I2STREAM_AGENT_PUBLIC_HOST", "")
    with pytest.raises(i2stream_console.ProxyRouteError) as unauthenticated:
        i2stream_console._require_privileged_access(target)
    assert unauthenticated.value.status == 503

    monkeypatch.setattr(auth, "is_auth_enabled", lambda: True)
    monkeypatch.setattr(i2stream_console, "I2STREAM_CONSOLE_BEARER_TOKEN", None)
    with pytest.raises(i2stream_console.ProxyRouteError) as unconfigured:
        i2stream_console._require_privileged_access(target)
    assert unconfigured.value.status == 503

    monkeypatch.setattr(i2stream_console, "I2STREAM_CONSOLE_BEARER_TOKEN", "x" * 32)
    i2stream_console._require_privileged_access(target)
    assert i2stream_console._install_callback_host(handler, target) == "192.168.34.65"
    for invalid_host in ("localhost:8787", "127.0.0.1:8787", "[::1]:8787", "bad host"):
        with pytest.raises(i2stream_console.ProxyRouteError):
            i2stream_console._install_callback_host(
                FakeHandler(headers={"Host": invalid_host}),
                target,
            )

    monkeypatch.setattr(i2stream_console, "I2STREAM_AGENT_PUBLIC_HOST", "10.20.30.40")
    assert i2stream_console._install_callback_host(
        FakeHandler(headers={"Host": "webui.example"}),
        target,
    ) is None


def test_knowledge_config_facade_requires_auth_and_internal_token_without_host_validation(monkeypatch):
    from api import auth, i2stream_console

    target = i2stream_console.resolve_proxy_target(
        "PUT",
        urlparse("/api/i2stream-console/knowledge/config"),
    )
    assert target.privileged_route is True
    assert target.install_route is False

    monkeypatch.setattr(auth, "is_auth_enabled", lambda: False)
    monkeypatch.setattr(i2stream_console, "I2STREAM_CONSOLE_BEARER_TOKEN", "x" * 32)
    with pytest.raises(i2stream_console.ProxyRouteError) as unauthenticated:
        i2stream_console._require_privileged_access(target)
    assert unauthenticated.value.status == 503

    monkeypatch.setattr(auth, "is_auth_enabled", lambda: True)
    monkeypatch.setattr(i2stream_console, "I2STREAM_CONSOLE_BEARER_TOKEN", None)
    with pytest.raises(i2stream_console.ProxyRouteError) as unconfigured:
        i2stream_console._require_privileged_access(target)
    assert unconfigured.value.status == 503

    monkeypatch.setattr(i2stream_console, "I2STREAM_CONSOLE_BEARER_TOKEN", "x" * 32)
    i2stream_console._require_privileged_access(target)
    assert i2stream_console._install_callback_host(
        FakeHandler(headers={"Host": "localhost:8787"}),
        target,
    ) is None


def test_install_facade_injects_server_credentials_and_browser_access_host(monkeypatch):
    from api import auth, i2stream_console

    body = b'{"target_ip":"192.168.34.67"}'
    response = FakeResponse(b'{"preflight_id":"abc"}')
    opener = FakeOpener(response)
    monkeypatch.setattr(auth, "is_auth_enabled", lambda: True)
    monkeypatch.setattr(i2stream_console, "_upstream_opener", lambda _origin: opener)
    monkeypatch.setattr(i2stream_console, "I2STREAM_CONSOLE_BASE_URL", "http://127.0.0.1:50091")
    monkeypatch.setattr(i2stream_console, "I2STREAM_CONSOLE_BEARER_TOKEN", "internal-install-token")
    monkeypatch.setattr(i2stream_console, "I2STREAM_AGENT_PUBLIC_HOST", "")
    handler = FakeHandler(
        body,
        headers={
            "Host": "192.168.34.65:8787",
            "Content-Length": str(len(body)),
            "Content-Type": "application/json",
            "Authorization": "Bearer browser-token",
            "X-I2Stream-Agent-Host": "203.0.113.10",
            "X-Forwarded-Host": "attacker.example",
        },
    )

    assert i2stream_console.handle_proxy(
        handler,
        urlparse("/api/i2stream-console/logmonitor/preflight"),
        "POST",
        read_request_body=True,
    ) is True

    request, _timeout = opener.requests[0]
    sent = {key.lower(): value for key, value in request.header_items()}
    assert request.data == body
    assert sent["authorization"] == "Bearer internal-install-token"
    assert sent["x-i2stream-agent-host"] == "192.168.34.65"
    assert "x-forwarded-host" not in sent
    assert "host" not in sent


@pytest.mark.parametrize(
    "target",
    [
        "http://127.0.0.1:50091/internal/gateway",
        "https://attacker.example/steal",
    ],
)
def test_upstream_redirects_cannot_escape_the_route_allowlist(target):
    from api.i2stream_console import _upstream_opener

    opener = _upstream_opener("http://127.0.0.1:50091")
    redirect_handler = next(
        handler for handler in opener.handlers if isinstance(handler, HTTPRedirectHandler)
    )
    request = SimpleNamespace(full_url="http://127.0.0.1:50091/api/reports")
    with pytest.raises(URLError, match="redirects are not allowed"):
        redirect_handler.redirect_request(request, None, 302, "Found", {}, target)


def test_report_content_is_streamed_in_bounded_chunks_and_response_is_closed(monkeypatch):
    from api import i2stream_console

    content = b"a" * (i2stream_console.STREAM_CHUNK_BYTES * 3 + 17)
    response = FakeResponse(
        content,
        headers={
            "Content-Type": "application/pdf",
            "Content-Length": str(len(content)),
            "Content-Disposition": 'attachment; filename="report.pdf"',
            "Set-Cookie": "upstream=secret",
        },
        forbid_unbounded_read=True,
    )
    opener = FakeOpener(response)
    monkeypatch.setattr(i2stream_console, "_upstream_opener", lambda _origin: opener)
    monkeypatch.setattr(i2stream_console, "I2STREAM_CONSOLE_BASE_URL", "http://127.0.0.1:50091")
    monkeypatch.setattr(i2stream_console, "I2STREAM_CONSOLE_BEARER_TOKEN", None)

    handler = FakeHandler(headers={"Range": "bytes=0-"})
    assert i2stream_console.handle_proxy(
        handler,
        urlparse("/api/i2stream-console/reports/abcdefghijklmnop/content"),
        "GET",
    ) is True

    request, _timeout = opener.requests[0]
    assert dict(request.header_items()) == {"Range": "bytes=0-"}
    assert handler.wfile.getvalue() == content
    assert handler.header("Content-Length") == str(len(content))
    assert handler.header("Content-Disposition") == 'inline; filename="report.pdf"'
    assert handler.header("Set-Cookie") is None
    assert handler.header("X-Frame-Options") is None
    assert "script-src 'none'" in handler.header("Content-Security-Policy")
    assert "frame-ancestors 'self'" in handler.header("Content-Security-Policy")
    assert response.read_sizes == [i2stream_console.STREAM_CHUNK_BYTES] * 5
    assert response.closed is True


def test_knowledge_upload_uses_the_configurable_upload_limit(monkeypatch):
    from api import i2stream_console

    response = FakeResponse(b'{"code":0,"status":"success"}')
    opener = FakeOpener(response)
    monkeypatch.setattr(i2stream_console, "_upstream_opener", lambda _origin: opener)
    monkeypatch.setattr(i2stream_console, "I2STREAM_CONSOLE_BASE_URL", "http://127.0.0.1:50091")
    monkeypatch.setattr(i2stream_console, "MAX_UPLOAD_BYTES", 32 * 1024 * 1024)
    body = b"x" * (i2stream_console.MAX_BODY_BYTES + 1)
    handler = FakeHandler(
        body,
        headers={
            "Content-Length": str(len(body)),
            "Content-Type": "multipart/form-data; boundary=test",
        },
    )

    assert i2stream_console.handle_proxy(
        handler,
        urlparse("/api/i2stream-console/knowledge/files"),
        "POST",
        read_request_body=True,
    ) is True
    request, _timeout = opener.requests[0]
    assert request.data == body


def test_logmonitor_install_request_allows_64kib_but_rejects_larger_body(monkeypatch):
    from api import auth, i2stream_console

    response = FakeResponse(b'{"preflight_id":"abc"}')
    opener = FakeOpener(response)
    monkeypatch.setattr(auth, "is_auth_enabled", lambda: True)
    monkeypatch.setattr(i2stream_console, "_upstream_opener", lambda _origin: opener)
    monkeypatch.setattr(i2stream_console, "I2STREAM_CONSOLE_BASE_URL", "http://127.0.0.1:50091")
    monkeypatch.setattr(i2stream_console, "I2STREAM_CONSOLE_BEARER_TOKEN", "internal-install-token")
    monkeypatch.setattr(i2stream_console, "I2STREAM_AGENT_PUBLIC_HOST", "192.168.34.65")

    accepted_body = b"x" * (64 * 1024)
    accepted = FakeHandler(
        accepted_body,
        headers={"Content-Length": str(len(accepted_body)), "Content-Type": "application/json"},
    )
    assert i2stream_console.handle_proxy(
        accepted,
        urlparse("/api/i2stream-console/logmonitor/preflight"),
        "POST",
        read_request_body=True,
    ) is True
    assert opener.requests[0][0].data == accepted_body

    rejected_body = b"x" * (64 * 1024 + 1)
    rejected = FakeHandler(
        rejected_body,
        headers={"Content-Length": str(len(rejected_body)), "Content-Type": "application/json"},
    )
    assert i2stream_console.handle_proxy(
        rejected,
        urlparse("/api/i2stream-console/logmonitor/preflight"),
        "POST",
        read_request_body=True,
    ) is True
    assert rejected.status == 413
    assert _json_body(rejected)["error"].startswith("Request body too large")
    assert len(opener.requests) == 1


def test_knowledge_config_put_reads_body_and_applies_small_limit(monkeypatch):
    from api import auth, i2stream_console

    response = FakeResponse(b'{"code":0,"status":"success"}')
    opener = FakeOpener(response)
    monkeypatch.setattr(auth, "is_auth_enabled", lambda: True)
    monkeypatch.setattr(i2stream_console, "_upstream_opener", lambda _origin: opener)
    monkeypatch.setattr(i2stream_console, "I2STREAM_CONSOLE_BASE_URL", "http://127.0.0.1:50091")
    monkeypatch.setattr(i2stream_console, "I2STREAM_CONSOLE_BEARER_TOKEN", "internal-config-token")

    body = b'{"vector_search_host":"http://rag:8900","rag_service_mcp_url":"http://rag:8900/mcp"}'
    accepted = FakeHandler(
        body,
        headers={"Content-Length": str(len(body)), "Content-Type": "application/json"},
    )
    assert i2stream_console.handle_proxy(
        accepted,
        urlparse("/api/i2stream-console/knowledge/config"),
        "PUT",
    ) is True
    request, _timeout = opener.requests[0]
    assert request.data == body
    assert request.full_url == "http://127.0.0.1:50091/api/knowledge/config"

    rejected_body = b"x" * (8 * 1024 + 1)
    rejected = FakeHandler(
        rejected_body,
        headers={"Content-Length": str(len(rejected_body)), "Content-Type": "application/json"},
    )
    assert i2stream_console.handle_proxy(
        rejected,
        urlparse("/api/i2stream-console/knowledge/config/check"),
        "POST",
    ) is True
    assert rejected.status == 413
    assert rejected.close_connection is True
    assert len(opener.requests) == 1


def test_knowledge_collection_create_forwards_small_json_body(monkeypatch):
    from api import auth, i2stream_console

    response = FakeResponse(b'{"code":0,"status":"success"}')
    opener = FakeOpener(response)
    monkeypatch.setattr(auth, "is_auth_enabled", lambda: True)
    monkeypatch.setattr(i2stream_console, "_upstream_opener", lambda _origin: opener)
    monkeypatch.setattr(i2stream_console, "I2STREAM_CONSOLE_BASE_URL", "http://127.0.0.1:50091")
    monkeypatch.setattr(i2stream_console, "I2STREAM_CONSOLE_BEARER_TOKEN", "internal-config-token")
    body = b'{"collection_name":"team-docs"}'
    handler = FakeHandler(
        body,
        headers={"Content-Length": str(len(body)), "Content-Type": "application/json"},
    )

    assert i2stream_console.handle_proxy(
        handler,
        urlparse("/api/i2stream-console/knowledge/collections"),
        "POST",
    ) is True

    request, _timeout = opener.requests[0]
    assert request.full_url == "http://127.0.0.1:50091/api/knowledge/collections"
    assert request.data == body


def test_delete_proxy_consumes_an_explicit_request_body(monkeypatch):
    from api import routes

    calls = []

    def fake_proxy(_handler, _parsed, method, *, read_request_body=False):
        calls.append((method, read_request_body))
        return True

    monkeypatch.setattr(routes, "_check_csrf", lambda _handler: True)
    monkeypatch.setattr("api.i2stream_console.handle_proxy", fake_proxy)
    assert routes.handle_delete(
        FakeHandler(b"{}", {"Content-Length": "2"}),
        urlparse("/api/i2stream-console/reports/abcdefghijklmnop"),
    ) is True
    assert calls == [("DELETE", True)]


def test_routes_apply_existing_csrf_gate_before_unsafe_proxy(monkeypatch):
    from api import routes

    called = False

    def should_not_proxy(*_args, **_kwargs):
        nonlocal called
        called = True
        return True

    monkeypatch.setattr(routes, "_check_csrf", lambda _handler: False)
    monkeypatch.setattr("api.i2stream_console.handle_proxy", should_not_proxy)
    handler = FakeHandler()

    assert routes.handle_post(
        handler,
        urlparse("/api/i2stream-console/knowledge/files"),
    ) is None
    assert handler.status == 403
    assert called is False


def test_routes_wire_get_and_delete_to_proxy(monkeypatch):
    from api import routes

    calls = []

    def fake_proxy(_handler, parsed, method, *, read_request_body=False):
        calls.append((parsed.path, method, read_request_body))
        return True

    monkeypatch.setattr("api.i2stream_console.handle_proxy", fake_proxy)
    get_handler = FakeHandler()
    delete_handler = FakeHandler()

    assert routes.handle_get(
        get_handler,
        urlparse("/api/i2stream-console/reports"),
    ) is True
    monkeypatch.setattr(routes, "_check_csrf", lambda _handler: True)
    assert routes.handle_delete(
        delete_handler,
        urlparse("/api/i2stream-console/reports/abcdefghijklmnop"),
    ) is True
    assert calls == [
        ("/api/i2stream-console/reports", "GET", False),
        ("/api/i2stream-console/reports/abcdefghijklmnop", "DELETE", True),
    ]


def test_webui_auth_gate_treats_console_proxy_as_protected_api(monkeypatch):
    from api import auth

    monkeypatch.setenv("HERMES_WEBUI_PASSWORD", "test-password")
    auth._invalidate_password_hash_cache()
    handler = FakeHandler()

    assert auth.check_auth(
        handler,
        SimpleNamespace(path="/api/i2stream-console/reports", query=""),
    ) is False
    assert handler.status == 401
    assert _json_body(handler) == {"error": "Authentication required"}
    auth._invalidate_password_hash_cache()


@pytest.mark.parametrize("raw", ["nan", "inf", "-inf", "0", "-1"])
def test_i2stream_timeout_rejects_non_finite_or_non_positive_values(monkeypatch, raw):
    from api.config import _positive_finite_env_float

    monkeypatch.setenv("TEST_I2STREAM_TIMEOUT", raw)
    with pytest.raises(ValueError, match="positive finite number"):
        _positive_finite_env_float("TEST_I2STREAM_TIMEOUT", "30")
