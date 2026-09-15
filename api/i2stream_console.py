"""Authenticated same-origin facade for the i2Stream compatibility service.

The WebUI server remains the browser-facing security boundary.  This module
proxies only knowledge, reports, node status, LogMonitor installation, and
client-scoped conversation history; agent streaming and the internal gateway
are intentionally not reachable.
"""

from __future__ import annotations

import logging
import re
from contextlib import closing
from dataclasses import dataclass
from ipaddress import ip_address
from urllib.error import HTTPError, URLError
from urllib.parse import (
    parse_qs,
    quote,
    unquote_to_bytes,
    urlencode,
    urlsplit,
)
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from api.config import (
    I2STREAM_AGENT_PUBLIC_HOST,
    I2STREAM_CONSOLE_BASE_URL,
    I2STREAM_CONSOLE_BEARER_TOKEN,
    I2STREAM_CONSOLE_TIMEOUT_SECONDS,
    MAX_UPLOAD_BYTES,
)
from api.helpers import (
    MAX_BODY_BYTES,
    _security_headers,
    bad,
    flush_pending_auth_cookies,
)

logger = logging.getLogger(__name__)

WEBUI_PREFIX = "/api/i2stream-console"
STREAM_CHUNK_BYTES = 64 * 1024
MAX_BUFFERED_RESPONSE_BYTES = 4 * 1024 * 1024
MAX_SAFE_MESSAGE_ID = (1 << 53) - 1
INSTALL_REQUEST_MAX_BYTES = 16 * 1024

_TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{16,128}$")
_CLIENT_ID_RE = re.compile(r"^[A-Za-z0-9_-]{16,128}$")
_BAD_PERCENT_RE = re.compile(r"%(?![0-9A-Fa-f]{2})")

_REQUEST_HEADER_ALLOWLIST = frozenset(
    {
        "accept",
        "content-type",
        "if-match",
        "if-none-match",
        "if-modified-since",
        "if-unmodified-since",
        "range",
    }
)
_RESPONSE_HEADER_ALLOWLIST = frozenset(
    {
        "accept-ranges",
        "content-disposition",
        "content-encoding",
        "content-range",
        "content-type",
        "etag",
        "last-modified",
    }
)


class ProxyRouteError(ValueError):
    """A request under the facade prefix is outside its public contract."""

    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


@dataclass(frozen=True)
class ProxyTarget:
    upstream_path: str
    upstream_query: str = ""
    extra_headers: dict[str, str] | None = None
    stream_response: bool = False
    install_route: bool = False

    def __post_init__(self) -> None:
        if self.extra_headers is None:
            object.__setattr__(self, "extra_headers", {})


def _decode_path_segment(raw: str, label: str, *, max_length: int = 512) -> str:
    if not raw or _BAD_PERCENT_RE.search(raw):
        raise ProxyRouteError(f"Invalid {label}")
    try:
        value = unquote_to_bytes(raw).decode("utf-8")
    except UnicodeDecodeError:
        raise ProxyRouteError(f"Invalid {label}") from None
    if (
        not value
        or len(value) > max_length
        or value in {".", ".."}
        or "/" in value
        or "\\" in value
        or "\x00" in value
    ):
        raise ProxyRouteError(f"Invalid {label}")
    return quote(value, safe="-._~")


def _require_no_query(parsed) -> None:
    if parsed.query:
        raise ProxyRouteError("Query parameters are not allowed for this endpoint")


def _single_query_value(query: dict[str, list[str]], name: str) -> str | None:
    values = query.get(name)
    if values is None:
        return None
    if len(values) != 1 or not values[0]:
        raise ProxyRouteError(f"Invalid {name}")
    return values[0]


def _parse_query(raw_query: str) -> dict[str, list[str]]:
    try:
        return parse_qs(raw_query, keep_blank_values=True, strict_parsing=True)
    except ValueError:
        raise ProxyRouteError("Malformed query string") from None


def _history_listing_target(parsed) -> ProxyTarget:
    query = _parse_query(parsed.query)
    unknown = set(query) - {"client_id", "limit", "before"}
    if unknown:
        raise ProxyRouteError("Unsupported conversation history query parameter")

    client_id = _single_query_value(query, "client_id")
    if client_id is None or not _CLIENT_ID_RE.fullmatch(client_id):
        raise ProxyRouteError("Invalid client_id")

    normalized: list[tuple[str, str]] = [("client_id", client_id)]
    raw_limit = _single_query_value(query, "limit")
    if raw_limit is not None:
        try:
            limit = int(raw_limit)
        except ValueError:
            raise ProxyRouteError("limit must be an integer from 1 to 100") from None
        if not 1 <= limit <= 100 or str(limit) != raw_limit:
            raise ProxyRouteError("limit must be an integer from 1 to 100")
        normalized.append(("limit", str(limit)))

    raw_before = _single_query_value(query, "before")
    if raw_before is not None:
        try:
            before = int(raw_before)
        except ValueError:
            raise ProxyRouteError("before must be a positive message id") from None
        if not 1 <= before <= MAX_SAFE_MESSAGE_ID or str(before) != raw_before:
            raise ProxyRouteError("before must be a positive message id")
        normalized.append(("before", str(before)))

    return ProxyTarget(
        upstream_path="/api/chat-conversations",
        upstream_query=urlencode(normalized),
    )


def _conversation_detail_target(parsed, raw_conversation_id: str) -> ProxyTarget:
    conversation_id = _decode_path_segment(
        raw_conversation_id,
        "conversation id",
        max_length=256,
    )
    query = _parse_query(parsed.query)
    if set(query) != {"client_id"}:
        raise ProxyRouteError("client_id is required and is the only supported query parameter")
    client_id = _single_query_value(query, "client_id")
    if client_id is None or not _CLIENT_ID_RE.fullmatch(client_id):
        raise ProxyRouteError("Invalid client_id")
    return ProxyTarget(
        upstream_path=f"/api/conversations/{conversation_id}/messages",
        extra_headers={"X-I2H-Client-Id": client_id},
    )


def resolve_proxy_target(method: str, parsed) -> ProxyTarget:
    """Resolve one browser route to an allowlisted compatibility-service route."""
    method = method.upper()
    path = parsed.path or ""
    if not (path == WEBUI_PREFIX or path.startswith(WEBUI_PREFIX + "/")):
        raise ProxyRouteError("Not an i2Stream console route", status=404)
    suffix = path[len(WEBUI_PREFIX):]

    if suffix == "/logmonitor/preflight":
        if method != "POST":
            raise ProxyRouteError("Method not allowed", status=405)
        _require_no_query(parsed)
        return ProxyTarget("/api/logmonitor/preflight", install_route=True)

    if suffix == "/logmonitor/installations":
        if method != "POST":
            raise ProxyRouteError("Method not allowed", status=405)
        _require_no_query(parsed)
        return ProxyTarget("/api/logmonitor/installations", install_route=True)

    match = re.fullmatch(r"/logmonitor/installations/([0-9a-f]{32})", suffix)
    if match:
        if method != "GET":
            raise ProxyRouteError("Method not allowed", status=405)
        _require_no_query(parsed)
        return ProxyTarget(
            f"/api/logmonitor/installations/{match.group(1)}",
            install_route=True,
        )

    if suffix == "/knowledge/files":
        if method not in {"GET", "POST"}:
            raise ProxyRouteError("Method not allowed", status=405)
        _require_no_query(parsed)
        return ProxyTarget("/api/knowledge/files")

    match = re.fullmatch(r"/knowledge/files/([^/]+)", suffix)
    if match:
        if method != "DELETE":
            raise ProxyRouteError("Method not allowed", status=405)
        _require_no_query(parsed)
        file_id = _decode_path_segment(match.group(1), "knowledge file id")
        return ProxyTarget(f"/api/knowledge/files/{file_id}")

    match = re.fullmatch(r"/knowledge/tasks/([^/]+)", suffix)
    if match:
        if method != "GET":
            raise ProxyRouteError("Method not allowed", status=405)
        _require_no_query(parsed)
        task_id = _decode_path_segment(match.group(1), "knowledge task id", max_length=256)
        return ProxyTarget(f"/api/knowledge/tasks/{task_id}")

    if suffix == "/reports":
        if method != "GET":
            raise ProxyRouteError("Method not allowed", status=405)
        _require_no_query(parsed)
        return ProxyTarget("/api/reports")

    if suffix == "/nodes":
        if method != "GET":
            raise ProxyRouteError("Method not allowed", status=405)
        _require_no_query(parsed)
        return ProxyTarget("/api/nodes")

    match = re.fullmatch(r"/nodes/([^/]+)", suffix)
    if match:
        if method != "DELETE":
            raise ProxyRouteError("Method not allowed", status=405)
        _require_no_query(parsed)
        encoded_ip = _decode_path_segment(match.group(1), "node IP", max_length=64)
        node_ip = unquote_to_bytes(encoded_ip).decode("utf-8")
        if "%" in node_ip:
            raise ProxyRouteError("Invalid node IP")
        try:
            canonical_ip = str(ip_address(node_ip))
        except ValueError:
            raise ProxyRouteError("Invalid node IP") from None
        return ProxyTarget(f"/api/nodes/{quote(canonical_ip, safe='')}")

    match = re.fullmatch(r"/reports/([^/]+)/content", suffix)
    if match:
        if method != "GET":
            raise ProxyRouteError("Method not allowed", status=405)
        _require_no_query(parsed)
        token = match.group(1)
        if not _TOKEN_RE.fullmatch(token):
            raise ProxyRouteError("Invalid report token")
        return ProxyTarget(f"/api/reports/{token}/content", stream_response=True)

    match = re.fullmatch(r"/reports/([^/]+)", suffix)
    if match:
        if method != "DELETE":
            raise ProxyRouteError("Method not allowed", status=405)
        _require_no_query(parsed)
        token = match.group(1)
        if not _TOKEN_RE.fullmatch(token):
            raise ProxyRouteError("Invalid report token")
        return ProxyTarget(f"/api/reports/{token}")

    if suffix == "/conversations":
        if method != "GET":
            raise ProxyRouteError("Method not allowed", status=405)
        return _history_listing_target(parsed)

    match = re.fullmatch(r"/conversations/([^/]+)/messages", suffix)
    if match:
        if method != "GET":
            raise ProxyRouteError("Method not allowed", status=405)
        return _conversation_detail_target(parsed, match.group(1))

    raise ProxyRouteError("Endpoint is not exposed by the i2Stream console facade", status=404)


def _validated_upstream_origin() -> str:
    raw = I2STREAM_CONSOLE_BASE_URL.rstrip("/")
    parts = urlsplit(raw)
    if (
        parts.scheme not in {"http", "https"}
        or not parts.hostname
        or parts.username is not None
        or parts.password is not None
        or parts.query
        or parts.fragment
        or parts.path not in {"", "/"}
    ):
        raise ValueError("HERMES_WEBUI_I2STREAM_CONSOLE_URL must be an HTTP(S) origin")
    try:
        _ = parts.port
    except ValueError:
        raise ValueError("HERMES_WEBUI_I2STREAM_CONSOLE_URL has an invalid port") from None
    return raw


def _upstream_opener(allowed_origin: str):
    class RejectRedirectHandler(HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            raise URLError("i2Stream compatibility redirects are not allowed")

    # Do not inherit HTTP(S)_PROXY for this explicitly configured internal hop.
    return build_opener(ProxyHandler({}), RejectRedirectHandler)


def _request_headers(
    handler,
    target: ProxyTarget,
    callback_host: str | None = None,
) -> dict[str, str]:
    headers: dict[str, str] = {}
    incoming = getattr(handler, "headers", None)
    if incoming and hasattr(incoming, "items"):
        for name, value in incoming.items():
            if str(name).lower() in _REQUEST_HEADER_ALLOWLIST:
                headers[str(name)] = str(value)
    headers.update(target.extra_headers)
    if callback_host is not None:
        headers["X-I2Stream-Agent-Host"] = callback_host
    if I2STREAM_CONSOLE_BEARER_TOKEN:
        headers["Authorization"] = f"Bearer {I2STREAM_CONSOLE_BEARER_TOKEN}"
    return headers


def _browser_access_ipv4(handler) -> str:
    raw_host = str(handler.headers.get("Host", "")).strip()
    if not raw_host or any(character.isspace() for character in raw_host):
        raise ProxyRouteError(
            "无法从浏览器访问地址确定 Agent IPv4，请配置 AGENT_PUBLIC_HOST",
            status=400,
        )
    parts = urlsplit("//" + raw_host)
    if (
        not parts.hostname
        or parts.username is not None
        or parts.password is not None
        or parts.path
        or parts.query
        or parts.fragment
    ):
        raise ProxyRouteError("浏览器访问地址不是有效的 IPv4 Host", status=400)
    try:
        _ = parts.port
        address = ip_address(parts.hostname)
    except ValueError:
        raise ProxyRouteError(
            "浏览器访问地址不是 IPv4，请配置 AGENT_PUBLIC_HOST",
            status=400,
        ) from None
    if address.version != 4 or address.is_loopback or address.is_unspecified or address.is_multicast:
        raise ProxyRouteError(
            "浏览器访问地址必须是非回环单播 IPv4，请配置 AGENT_PUBLIC_HOST",
            status=400,
        )
    return str(address)


def _install_callback_host(handler, target: ProxyTarget) -> str | None:
    if not target.install_route:
        return None
    from api.auth import is_auth_enabled

    if not is_auth_enabled():
        raise ProxyRouteError("启用 WebUI 登录保护后才能使用远程安装", status=503)
    if not I2STREAM_CONSOLE_BEARER_TOKEN:
        raise ProxyRouteError("LogMonitor 安装服务未配置内部凭据", status=503)
    if I2STREAM_AGENT_PUBLIC_HOST:
        return None
    return _browser_access_ipv4(handler)


def _read_request_body(handler, max_bytes: int) -> bytes:
    raw_length = handler.headers.get("Content-Length", 0)
    try:
        length = int(raw_length)
    except (TypeError, ValueError):
        raise ProxyRouteError("Invalid Content-Length") from None
    if length < 0:
        raise ProxyRouteError("Invalid Content-Length")
    if length > max_bytes:
        raise ProxyRouteError(
            f"Request body too large ({length} bytes, max {max_bytes})",
            status=413,
        )
    body = handler.rfile.read(length)
    if len(body) != length:
        raise ProxyRouteError("Request body ended before Content-Length bytes were received")
    return body


def _response_header(response_headers, name: str) -> str | None:
    if not response_headers or not hasattr(response_headers, "get"):
        return None
    value = response_headers.get(name)
    if value is None:
        return None
    value = str(value)
    if "\r" in value or "\n" in value:
        raise ValueError(f"Invalid upstream {name} header")
    return value


def _report_content_security_headers(handler) -> None:
    handler.send_header("X-Content-Type-Options", "nosniff")
    handler.send_header("Referrer-Policy", "no-referrer")
    handler.send_header(
        "Content-Security-Policy",
        "default-src 'none'; img-src 'self' data:; style-src 'unsafe-inline'; "
        "script-src 'none'; base-uri 'none'; form-action 'none'; frame-ancestors 'self'",
    )
    handler.send_header(
        "Permissions-Policy",
        "camera=(), microphone=(), geolocation=(), clipboard-write=()",
    )


def _begin_response(
    handler,
    status: int,
    response_headers,
    *,
    content_length: int | None,
    report_content: bool = False,
) -> None:
    handler.send_response(status)
    if response_headers and hasattr(response_headers, "items"):
        for name, _value in response_headers.items():
            lower_name = str(name).lower()
            if lower_name not in _RESPONSE_HEADER_ALLOWLIST:
                continue
            safe_value = _response_header(response_headers, str(name))
            if safe_value is not None:
                if report_content and lower_name == "content-disposition":
                    disposition, separator, parameters = safe_value.partition(";")
                    if disposition.strip().lower() == "attachment":
                        safe_value = "inline" + (separator + parameters if separator else "")
                handler.send_header(str(name), safe_value)
    if content_length is not None:
        handler.send_header("Content-Length", str(content_length))
    else:
        handler.send_header("Connection", "close")
        handler.close_connection = True
    handler.send_header("Cache-Control", "no-store")
    if report_content:
        _report_content_security_headers(handler)
    else:
        _security_headers(handler)
    flush_pending_auth_cookies(handler)
    handler.end_headers()


def _read_bounded_response(response) -> bytes:
    body = response.read(MAX_BUFFERED_RESPONSE_BYTES + 1)
    if len(body) > MAX_BUFFERED_RESPONSE_BYTES:
        raise ValueError("i2Stream compatibility response too large")
    return body


def _send_buffered_response(handler, status: int, response_headers, body: bytes) -> None:
    _begin_response(handler, status, response_headers, content_length=len(body))
    handler.wfile.write(body)


def _stream_response(handler, status: int, response_headers, response) -> None:
    raw_length = _response_header(response_headers, "Content-Length")
    content_length = None
    if raw_length is not None:
        try:
            content_length = int(raw_length)
        except ValueError:
            raise ValueError("Invalid upstream Content-Length") from None
        if content_length < 0:
            raise ValueError("Invalid upstream Content-Length")

    _begin_response(
        handler,
        status,
        response_headers,
        content_length=content_length,
        report_content=True,
    )
    written = 0
    while True:
        chunk = response.read(STREAM_CHUNK_BYTES)
        if not chunk:
            break
        handler.wfile.write(chunk)
        handler.wfile.flush()
        written += len(chunk)
    if content_length is not None and written != content_length:
        handler.close_connection = True
        logger.warning(
            "i2Stream report stream length mismatch: expected=%s written=%s",
            content_length,
            written,
        )


def handle_proxy(
    handler,
    parsed,
    method: str,
    *,
    read_request_body: bool = False,
) -> bool:
    """Proxy a request under ``WEBUI_PREFIX``; return ``False`` otherwise."""
    path = parsed.path or ""
    if not (path == WEBUI_PREFIX or path.startswith(WEBUI_PREFIX + "/")):
        return False

    try:
        target = resolve_proxy_target(method, parsed)
        callback_host = _install_callback_host(handler, target)
        origin = _validated_upstream_origin()
        if read_request_body:
            request_limit = (
                MAX_UPLOAD_BYTES
                if method.upper() == "POST" and target.upstream_path == "/api/knowledge/files"
                else (
                    INSTALL_REQUEST_MAX_BYTES
                    if target.install_route
                    else MAX_BODY_BYTES
                )
            )
            request_body = _read_request_body(handler, request_limit)
        else:
            request_body = None
        upstream_url = origin + target.upstream_path
        if target.upstream_query:
            upstream_url += "?" + target.upstream_query
        request = Request(
            upstream_url,
            data=request_body,
            headers=_request_headers(handler, target, callback_host),
            method=method.upper(),
        )
        opener = _upstream_opener(origin)
        with opener.open(request, timeout=I2STREAM_CONSOLE_TIMEOUT_SECONDS) as response:
            status = getattr(response, "status", 200)
            if target.stream_response:
                _stream_response(handler, status, response.headers, response)
            else:
                body = _read_bounded_response(response)
                _send_buffered_response(handler, status, response.headers, body)
        return True
    except ProxyRouteError as exc:
        if read_request_body:
            handler.close_connection = True
        bad(handler, str(exc), status=exc.status)
        return True
    except HTTPError as exc:
        try:
            with closing(exc):
                body = _read_bounded_response(exc)
                _send_buffered_response(handler, exc.code, exc.headers, body)
        except ValueError as read_exc:
            bad(handler, str(read_exc), status=502)
        return True
    except (TimeoutError, URLError, OSError, ValueError):
        logger.warning(
            "i2Stream console proxy failed for %s %s",
            method,
            path,
            exc_info=True,
        )
        bad(handler, "Failed to reach i2Stream compatibility service", status=502)
        return True
