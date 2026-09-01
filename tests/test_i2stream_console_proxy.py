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
