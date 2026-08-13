from __future__ import annotations

import re

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


def validate_client_id(client_id: str) -> str:
    if not CLIENT_ID_PATTERN.fullmatch(client_id):
        raise HTTPException(status_code=400, detail="Invalid client id")
    return client_id


def validate_request_id(request_id: str) -> str:
    if not REQUEST_ID_PATTERN.fullmatch(request_id):
        raise HTTPException(status_code=400, detail="Invalid request id")
    return request_id
