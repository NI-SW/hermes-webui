"""Server-to-server client for native WebUI DataCop feedback jobs."""

from __future__ import annotations

import json
from contextlib import closing
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from api.config import (
    I2STREAM_CONSOLE_BASE_URL,
    I2STREAM_CONSOLE_TIMEOUT_SECONDS,
    I2STREAM_FEEDBACK_BRIDGE_TOKEN,
)


MAX_RESPONSE_BYTES = 1024 * 1024


class FeedbackServiceError(RuntimeError):
    def __init__(self, message: str, *, status: int = 502):
        super().__init__(message)
        self.status = status


class _RejectRedirectHandler(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise URLError("WebUI feedback redirects are not allowed")


def _origin() -> str:
    raw = I2STREAM_CONSOLE_BASE_URL.rstrip("/")
    parts = urlsplit(raw)
    try:
        parts.port
    except ValueError as exc:
        raise FeedbackServiceError("Invalid i2Stream feedback service origin") from exc
    if (
        parts.scheme not in {"http", "https"}
        or not parts.hostname
        or parts.username is not None
        or parts.password is not None
        or parts.path not in {"", "/"}
        or parts.query
        or parts.fragment
    ):
        raise FeedbackServiceError("Invalid i2Stream feedback service origin")
    return raw


def _decode_response(response) -> dict[str, object]:
    body = response.read(MAX_RESPONSE_BYTES + 1)
    if len(body) > MAX_RESPONSE_BYTES:
        raise FeedbackServiceError("i2Stream feedback response is too large")
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise FeedbackServiceError("i2Stream feedback response is not valid JSON") from exc
    if not isinstance(payload, dict):
        raise FeedbackServiceError("i2Stream feedback response must be an object")
    return payload


def _request(
    path: str,
    *,
    method: str,
    payload: dict[str, object] | None = None,
) -> dict[str, object]:
    if not I2STREAM_FEEDBACK_BRIDGE_TOKEN:
        raise FeedbackServiceError("WebUI feedback bridge token is not configured")
    data = None
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    request = Request(
        _origin() + path,
        data=data,
        method=method,
        headers={
            "Accept": "application/json",
            "Content-Type": "application/json",
            "Authorization": f"Bearer {I2STREAM_FEEDBACK_BRIDGE_TOKEN}",
        },
    )
    opener = build_opener(ProxyHandler({}), _RejectRedirectHandler())
    try:
        with opener.open(request, timeout=I2STREAM_CONSOLE_TIMEOUT_SECONDS) as response:
            return _decode_response(response)
    except HTTPError as exc:
        with closing(exc):
            try:
                body = _decode_response(exc)
            except FeedbackServiceError:
                body = {}
        detail = body.get("detail") if isinstance(body, dict) else None
        message = (
            str(detail)
            if isinstance(detail, str) and detail
            else "Feedback service rejected the request"
        )
        status = exc.code if 400 <= exc.code < 500 else 502
        raise FeedbackServiceError(message, status=status) from exc
    except (TimeoutError, URLError, OSError) as exc:
        raise FeedbackServiceError("Failed to reach i2Stream feedback service") from exc


def submit_webui_feedback(
    source_instance_id: str,
    session_id: str,
    message_ref: str,
    feedback: str,
    messages: list[dict[str, object]],
) -> dict[str, object]:
    path = (
        "/api/webui/sessions/"
        + quote(session_id, safe="")
        + "/messages/"
        + quote(message_ref, safe="")
        + "/feedback"
    )
    return _request(
        path,
        method="POST",
        payload={
            "source_instance_id": source_instance_id,
            "feedback": feedback,
            "messages": messages,
        },
    )


def get_webui_feedback_job(source_instance_id: str, job_id: str) -> dict[str, object]:
    query = urlencode({"source_instance_id": source_instance_id})
    return _request(
        "/api/webui/dialog-interactions/" + quote(job_id, safe="") + "?" + query,
        method="GET",
    )
