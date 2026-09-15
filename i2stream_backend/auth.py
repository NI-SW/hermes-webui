from __future__ import annotations

import re
import secrets

from fastapi import Header, HTTPException

from config import settings


TOKEN_PATTERN = re.compile(r"^[A-Za-z0-9_-]{16,128}$")
CLIENT_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{16,128}$")
REQUEST_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{16,128}$")


def require_proxy_auth(authorization: str | None = Header(default=None)) -> None:
    if not settings.proxy_api_key:
        return
    expected = f"Bearer {settings.proxy_api_key}"
    if authorization != expected:
        raise HTTPException(status_code=401, detail="Invalid proxy authorization")


def require_install_auth(authorization: str | None = Header(default=None)) -> None:
    token = settings.i2stream_install_internal_token.get_secret_value()
    if not token:
        raise HTTPException(status_code=503, detail="LogMonitor installation is not configured")
    expected = f"Bearer {token}"
    if authorization is None or not secrets.compare_digest(authorization, expected):
        raise HTTPException(status_code=401, detail="Invalid installation authorization")


def require_webui_feedback_auth(
    authorization: str | None = Header(default=None),
) -> None:
    token = settings.webui_feedback_bridge_token.get_secret_value()
    if not token:
        raise HTTPException(status_code=503, detail="WebUI feedback bridge is not configured")
    expected = f"Bearer {token}"
    if authorization is None or not secrets.compare_digest(authorization, expected):
        raise HTTPException(status_code=401, detail="Invalid WebUI feedback authorization")


def validate_client_id(client_id: str) -> str:
    if not CLIENT_ID_PATTERN.fullmatch(client_id):
        raise HTTPException(status_code=400, detail="Invalid client id")
    return client_id


def validate_request_id(request_id: str) -> str:
    if not REQUEST_ID_PATTERN.fullmatch(request_id):
        raise HTTPException(status_code=400, detail="Invalid request id")
    return request_id
