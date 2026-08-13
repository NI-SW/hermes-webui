"""Dashboard 无登录场景下的匿名 Cookie 身份。"""

from __future__ import annotations

import hashlib
import hmac
import re
import secrets

from fastapi import Response
from pydantic import SecretStr

DASHBOARD_USER_COOKIE_NAME = "agent_console_dashboard_user"
DASHBOARD_USER_COOKIE_MAX_AGE_SECONDS = 400 * 24 * 60 * 60
_COOKIE_PATTERN = re.compile(r"^([0-9a-f]{64})\.([0-9a-f]{64})$")
_SIGNATURE_CONTEXT = b"agent-console-dashboard-user:v1\0"


class DashboardAnonymousIdentity:
    def __init__(self, secret: SecretStr) -> None:
        self._secret = secret

    def resolve(self, cookie_value: str | None) -> tuple[str, str]:
        if cookie_value is not None:
            match = _COOKIE_PATTERN.fullmatch(cookie_value)
            if match is not None:
                user_id, signature = match.groups()
                if hmac.compare_digest(signature, self._signature(user_id)):
                    return user_id, cookie_value

        user_id = secrets.token_hex(32)
        return user_id, f"{user_id}.{self._signature(user_id)}"

    @staticmethod
    def set_cookie(response: Response, cookie_value: str, *, secure: bool) -> None:
        response.set_cookie(
            key=DASHBOARD_USER_COOKIE_NAME,
            value=cookie_value,
            max_age=DASHBOARD_USER_COOKIE_MAX_AGE_SECONDS,
            path="/",
            secure=secure,
            httponly=True,
            samesite="lax",
        )

    def _signature(self, user_id: str) -> str:
        return hmac.new(
            self._secret.get_secret_value().encode("utf-8"),
            _SIGNATURE_CONTEXT + user_id.encode("ascii"),
            hashlib.sha256,
        ).hexdigest()
